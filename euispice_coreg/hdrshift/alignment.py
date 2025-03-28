import copy

import numpy as np
import multiprocessing as mp
from functools import partial
from astropy.coordinates import SkyCoord
from tqdm import tqdm
from multiprocessing import Process, Lock
import astropy.io.fits as Fits
from . import c_correlate
from ..utils import rectify
from astropy.wcs import WCS, FITSFixedWarning
import astropy.units as u
from ..plot import plot
import warnings
from ..utils import Util
from astropy.wcs.utils import WCS_FRAME_MAPPINGS, FRAME_WCS_MAPPINGS
from multiprocessing import Process, Queue
import traceback
from astropy.time import Time

warnings.filterwarnings('ignore', category=FITSFixedWarning, append=True)


def divide_chunks(l, n):
    # looping till length l
    for i in range(0, len(l), n):
        yield l[i:i + n]


class Alignment:

    def __init__(self, large_fov_known_pointing:str, small_fov_to_correct:str, lag_crval1: np.array,
                 lag_crval2: np.array, lag_cdelta1: object, lag_cdelta2: object, lag_crota: object,
                 lag_solar_r: object = None,
                 small_fov_value_min: object = None,
                 parallelism: object = False, use_tqdm: object = False,
                 small_fov_value_max: object = None, counts_cpu_max: int = 40, large_fov_window: object = -1,
                 small_fov_window: object = -1,
                 path_save_figure: object = None, reprojection_order=2, force_crota_0=False):
        """

        @param large_fov_known_pointing: (str) path to the reference file fits (most of the time an imager or a synthetic raster)
        @param small_fov_to_correct: (str)  path to the fits file to align. Only the header values will be changed.
        @param lag_crval1: (arcsec) array of header CRVAL1 lags.
        @param lag_crval2: (arcsec) array of header CRVAL2 lags.
        @param lag_cdelta1: (arcsec) array of header CDELT1 lags.
        @param lag_cdelta2: (arcsec) array of header CDELT2 lags.
        @param lag_crota: (deg) array of header CROTA lags. the PC1_1/2 matrixes will be updated accordingly.
        @param lag_solar_r: ([1/Rsun]) set to 1.004 by default. Only needed if apply carrington transformation.
        Important: If you align PHI data, you should set it at 1.000 .
        @param small_fov_value_min: min value (optional)
        @param small_fov_value_max: max value (optional)
        @param parallelism: set true to allow parallelism.
        @param use_tqdm: show advancement bar in terminal.
        @param counts_cpu_max: allow max number of cpu for the parallelism.
        @param large_fov_window: (str or int) HDULIST window for the reference file
        @param small_fov_window: (str or int) HDULIST window for the fits to align
        @param path_save_figure: folder where to save figs following the alignement (optional, will increase computational time)
        @param reprojection_order: (int) order of the spline interpolation. Default is 2.
        @param force_crota_0: if no CROTA, CROTA2 or Pci_j matrix, force the CROTA parameter to 0.
        """
        self.large_fov_known_pointing = large_fov_known_pointing
        self.small_fov_to_correct = small_fov_to_correct
        self.lag_crval1 = lag_crval1
        self.lag_crval2 = lag_crval2
        self.lag_cdelta1 = lag_cdelta1
        self.lag_cdelta2 = lag_cdelta2

        self.lag_crota = lag_crota
        self.lag_solar_r = lag_solar_r
        self.lonlims = None
        self.latlims = None
        self.shape = None
        self.reference_date = None
        self.parallelism = parallelism
        self.small_fov_window = small_fov_window
        self.large_fov_window = large_fov_window

        self.crval1_ref = None
        self.crval2_ref = None
        self.crota_ref = None
        self.cdelta_ref = None
        self.data_large = None
        self.counts = counts_cpu_max
        self.data_small = None
        self.hdr_small = None
        self.hdr_large = None
        self.method = None
        self.rat_wave = {'171': '171', '193': '195', '211': '195', '131': '171', '304': '304', '335': '304',
                         '94': '171', '174': '171'}
        self.small_fov_value_min = small_fov_value_min
        self.small_fov_value_max = small_fov_value_max
        self.path_save_figure = path_save_figure
        self.use_tqdm = use_tqdm
        self.marker = False
        self.force_crota_0 = force_crota_0
        self._large = None
        self._small = None

        self.use_pcij = True
        if (lag_crota is None) and (lag_cdelta1 is None) and (lag_cdelta2 is None):
            self.use_pcij = False

        self._correlation = None

        self.order = reprojection_order

        self.lock = Lock()

        # check whether the Helioprojective frame is imported through an sunpy.map import for instance.
        use_sunpy = False
        for mapping in [WCS_FRAME_MAPPINGS, FRAME_WCS_MAPPINGS]:
            if mapping[-1][0].__module__ == 'sunpy.coordinates.wcs_utils':
                use_sunpy = True
                # import sunpy.map
        self.use_sunpy = use_sunpy
        # set None values to np.array([0]) lags.
        for lag_name, lag_value in zip(["lag_crval1", "lag_crval2", "lag_crota", "lag_cdelta1", "lag_cdelta2"],
                                       [lag_crval1, lag_crval2, lag_crota, lag_cdelta1, lag_cdelta2]):
            if lag_value is None:
                self.__setattr__(lag_name, np.array([0.0]))

    # def __del__(self):

    def _shift_header(self, hdr, **kwargs):
        if 'd_crval1' in kwargs.keys():
            if self.unit_lag == hdr["CUNIT1"]:
                hdr['CRVAL1'] = self.crval1_ref + kwargs["d_crval1"]
            else:
                hdr['CRVAL1'] = u.Quantity(self.crval1_ref, self.unit_lag).to(hdr["CUNIT1"]).value \
                                + u.Quantity(kwargs["d_crval1"], self.unit_lag).to(hdr["CUNIT1"]).value

        if 'd_crval2' in kwargs.keys():
            if self.unit_lag == hdr["CUNIT2"]:
                hdr['CRVAL2'] = self.crval2_ref + kwargs["d_crval2"]
            else:
                hdr['CRVAL2'] = u.Quantity(self.crval2_ref, self.unit_lag).to(hdr["CUNIT2"]).value \
                                + u.Quantity(kwargs["d_crval2"], self.unit_lag).to(hdr["CUNIT2"]).value
        change_pcij = False

        if ('d_cdelta1' in kwargs.keys()):
            if kwargs["d_cdelta1"] != 0.0:
                change_pcij = True
                cdelt1 = (u.Quantity(self.cdelta1_ref, self.unit_lag)
                          + u.Quantity(kwargs["d_cdelta1"], self.unit_lag))
                hdr['CDELT1'] = cdelt1.to(hdr["CUNIT1"]).value
        if 'd_cdelta2' in kwargs.keys():
            if kwargs["d_cdelta2"] != 0.0:
                change_pcij = True

                cdelt2 = (u.Quantity(self.cdelta2_ref, self.unit_lag)
                          + u.Quantity(kwargs["d_cdelta2"], self.unit_lag))
                hdr['CDELT2'] = cdelt2.to(hdr["CUNIT2"]).value
        if 'd_crota' in kwargs.keys():
            if kwargs["d_crota"] != 0.0:
                change_pcij = True

                if 'CROTA' in hdr:
                    hdr['CROTA'] = self.crota_ref + kwargs["d_crota"]
                    # crot = hdr['CROTA']
                elif 'CROTA2' in hdr:
                    hdr['CROTA2'] = self.crota_ref + kwargs["d_crota"]
                    # crot = hdr['CROTA2']
                else:
                    if kwargs["d_crota"] != 0.0:
                        crot = np.rad2deg(np.arccos(hdr["PC1_1"]))
                        s = - np.sign(hdr["PC1_2"]) + (hdr["PC1_2"] == 0.0)
                        crot = crot * s
                        hdr["CROTA"] = crot
            if kwargs["d_crota"] != 0.0:
                crot = self.crota_ref + kwargs["d_crota"]
            else:
                crot = self.crota_ref
            # raise NotImplementedError
        if change_pcij:
            rho = np.deg2rad(crot)
            lam = hdr["CDELT2"] / hdr["CDELT1"]
            hdr["PC1_1"] = np.cos(rho)
            hdr["PC2_2"] = np.cos(rho)
            hdr["PC1_2"] = - lam * np.sin(rho)
            hdr["PC2_1"] = (1 / lam) * np.sin(rho)

    def _iteration_step_along_crval2(self, d_crval1, d_cdelta1, d_cdelta2, d_crota, d_solar_r, method: str,
                                     position: tuple, lock=None,error_queue = None):
        try:
            results = np.zeros(len(self.lag_crval2), dtype=np.float64)
            if self.use_tqdm:
                for ii, d_crval2 in enumerate(tqdm(self.lag_crval2, desc='crval1 = %.2f' % (d_crval1))):
                    results[ii] = self._step(d_crval2=d_crval2, d_crval1=d_crval1,
                                            d_cdelta1=d_cdelta1, d_cdelta2=d_cdelta2, d_crota=d_crota,
                                            method=method, d_solar_r=d_solar_r,
                                            )

            else:

                for ii, d_crval2 in enumerate(self.lag_crval2):
                    results[ii] = self._step(d_crval2=d_crval2, d_crval1=d_crval1,
                                            d_cdelta1=d_cdelta1, d_cdelta2=d_cdelta2, d_crota=d_crota,
                                            method=method, d_solar_r=d_solar_r,
                                            )

            lock.acquire()
            shmm_correlation, data_correlation = Util.MpUtils.gen_shmm(create=False, **self._correlation)
            data_correlation[position[0], :, position[1], position[2], position[3], position[4]] = results
            lock.release()
            shmm_correlation.close()
        except Exception as e:
            if error_queue is not None:
                error_queue.put((e, traceback.format_exc()))
            else: 
                raise e
        
    def _step(self, d_crval2, d_crval1, d_cdelta1, d_cdelta2, d_crota, d_solar_r, method: str, ):

        shmm_small, data_small = Util.MpUtils.gen_shmm(create=False, **self._small)
        shmm_large, data_large = Util.MpUtils.gen_shmm(create=False, **self._large)

        hdr_small_shft = self.hdr_small.copy()
        self._shift_header(hdr_small_shft, d_crval1=d_crval1, d_crval2=d_crval2,
                           d_cdelta1=d_cdelta1, d_cdelta2=d_cdelta2,
                           d_crota=d_crota)

        data_small_interp = self.function_to_apply(d_solar_r=d_solar_r, data=data_small, hdr=hdr_small_shft)
        data_small_interp = copy.deepcopy(data_small_interp)

        condition_1 = np.ones(len(data_small_interp.ravel()), dtype='bool')
        condition_2 = np.ones(len(data_small_interp.ravel()), dtype='bool')

        if self.small_fov_value_min is not None:
            condition_1 = np.array(data_small_interp.ravel() > self.small_fov_value_min, dtype='bool')
        if self.small_fov_value_max is not None:
            condition_2 = np.array(data_small_interp.ravel() < self.small_fov_value_max, dtype='bool')

        if method == 'correlation':

            lag = [0]
            is_nan = np.array((np.isnan(data_large.ravel(), dtype='bool')
                               | (np.isnan(data_small_interp.ravel(), dtype='bool'))),
                              dtype='bool')
            c = c_correlate.c_correlate(data_large.ravel()[(~is_nan) & (condition_1) & (condition_2)],
                                        data_small_interp.ravel()[(~is_nan) & (condition_1) & (condition_2)],
                                        lags=lag)
            # print(f'{data_large=}')
            # l = data_small_interp.shape
            # print(f'{data_small_interp[l[0]//2, l[1]//2]=}')
            # print(f'{c=}')

            c = copy.deepcopy(c)
            shmm_large.close()
            shmm_small.close()

            return c

        elif method == 'residus':
            norm = np.sqrt(data_large.ravel())
            diff = (data_large.ravel() - data_small_interp.ravel()) / norm
            return np.std(diff[(condition_1) & (condition_2)])
        else:
            raise NotImplementedError

    def _step_no_shmm(self, d_crval2, d_crval1, d_cdelta1, d_cdelta2, d_crota, d_solar_r, method: str, ):

        data_small = self.data_small.copy()
        data_large = self.data_large
        hdr_small_shft = self.hdr_small.copy()
        self._shift_header(hdr_small_shft, d_crval1=d_crval1, d_crval2=d_crval2,
                           d_cdelta1=d_cdelta1, d_cdelta2=d_cdelta2,
                           d_crota=d_crota)

        data_small_interp = self.function_to_apply(d_solar_r=d_solar_r, data=data_small, hdr=hdr_small_shft)

        condition_1 = np.ones(len(data_small_interp.ravel()), dtype='bool')
        condition_2 = np.ones(len(data_small_interp.ravel()), dtype='bool')

        if self.small_fov_value_min is not None:
            condition_1 = np.array(data_small_interp.ravel() > self.small_fov_value_min, dtype='bool')
        if self.small_fov_value_max is not None:
            condition_2 = np.array(data_small_interp.ravel() < self.small_fov_value_max, dtype='bool')

        if method == 'correlation':

            lag = [0]
            is_nan = np.array((np.isnan(data_large.ravel(), dtype='bool')
                               | (np.isnan(data_small_interp.ravel(), dtype='bool'))),
                              dtype='bool')
            c = c_correlate.c_correlate(data_large.ravel()[(~is_nan) & (condition_1) & (condition_2)],
                                        data_small_interp.ravel()[(~is_nan) & (condition_1) & (condition_2)],
                                        lags=lag)
            return c

        elif method == 'residus':
            norm = np.sqrt(data_large.ravel())
            diff = (data_large.ravel() - data_small_interp.ravel()) / norm
            return np.std(diff[(condition_1) & (condition_2)])
        else:
            raise NotImplementedError

    def align_using_carrington(self, lonlims=None, latlims=None, size_deg_carrington=None, shape=None,
                               reference_date=None, method='correlation'):

        self.reference_date = reference_date
        self.function_to_apply = self._carrington_transform
        self.method = method
        self.coordinate_frame = "carrington"

        f_large = Fits.open(self.large_fov_known_pointing)
        f_small = Fits.open(self.small_fov_to_correct)

        self.data_large = np.array(f_large[self.large_fov_window].data.copy(), dtype=np.float64)
        self.hdr_large = f_large[self.large_fov_window].header.copy()
        # self._recenter_crpix_in_header(self.hdr_large)

        self.hdr_small = f_small[self.small_fov_window].header.copy()
        # self._recenter_crpix_in_header(self.hdr_small)

        self.data_small = np.array(f_small[self.small_fov_window].data.copy(), dtype=np.float64)

        if (lonlims is None) and (latlims is None) & (size_deg_carrington is not None):

            CRLN_OBS = self.hdr_small["CRLN_OBS"]
            CRLT_OBS = self.hdr_small["CRLT_OBS"]

            self.lonlims = [CRLN_OBS - 0.5 * size_deg_carrington[0], CRLN_OBS + 0.5 * size_deg_carrington[0]]
            self.latlims = [CRLT_OBS - 0.5 * size_deg_carrington[1], CRLT_OBS + 0.5 * size_deg_carrington[1]]
            self.shape = [self.hdr_small["NAXIS1"], self.hdr_small["NAXIS2"]]
            print(f"{self.lonlims=}")

        elif (lonlims is not None) and (latlims is not None) & (shape is not None):

            self.lonlims = lonlims
            self.latlims = latlims
            self.shape = shape
        else:
            raise ValueError("either set lonlims as None, or not. no in between.")

        # if self.use_pcij:
        self._check_ant_create_pcij_matrix(self.hdr_small)
        self._check_ant_create_pcij_matrix(self.hdr_large)

        f_large.close()
        f_small.close()
        results = self._find_best_header_parameters()
        return results

    def align_using_helioprojective(self, method='correlation', correct_shift_solar_rotation=False):

        self.lonlims = None
        self.latlims = None
        self.shape = None
        self.reference_date = None
        self.function_to_apply = self._interpolate_on_large_data_grid

        self.method = method
        self.coordinate_frame = "helioprojective"
        f_large = Fits.open(self.large_fov_known_pointing)
        f_small = Fits.open(self.small_fov_to_correct)
        dat_large_var = np.array(f_large[self.large_fov_window].data.copy(), dtype=np.float64)
        self.data_large = dat_large_var

        self.hdr_large = f_large[self.large_fov_window].header.copy()
        # self._recenter_crpix_in_header(self.hdr_large)

        self.hdr_small = f_small[self.small_fov_window].header.copy()

        # if self.use_pcij:
        self._check_ant_create_pcij_matrix(self.hdr_small)
        self._check_ant_create_pcij_matrix(self.hdr_large)

        # self._recenter_crpix_in_header(self.hdr_small)
        self.data_small = np.array(f_small[self.small_fov_window].data.copy(), dtype=np.float64)
        f_large.close()
        f_small.close()

        results = self._find_best_header_parameters()

        return results

    def _check_ant_create_pcij_matrix(self, hdr):
        if ("PC1_1" not in hdr):
            warnings.warn("PCi_j matrix not found in header of the FITS file to align. Adding it to the header.")
            if "CROTA" in hdr:
                crot = hdr["CROTA"]
            elif "CROTA2" in hdr:
                crot = hdr["CROTA2"]
            else:
                if self.force_crota_0:
                    crot = 0.0
                    hdr["CROTA"] = 0.0
                else:
                    raise ValueError("No, CROTA, CROTA2 or PCi_j matrix in your FITS file. If want to force a CROTA=0, "
                                     "please set the force_crota_0 to True when initializing Alignment ")

            rho = np.deg2rad(crot)
            lam = hdr["CDELT2"] / hdr["CDELT1"]
            hdr["PC1_1"] = np.cos(rho)
            hdr["PC2_2"] = np.cos(rho)
            hdr["PC1_2"] = - lam * np.sin(rho)
            hdr["PC2_1"] = (1 / lam) * np.sin(rho)
        if hdr["PC1_1"] > 1.0:
            warnings.warn(f'{hdr["PC1_1"]=}, setting to  1.0.')
            hdr["PC1_1"] = 1.0
            hdr["PC2_2"] = 1.0
            hdr["PC1_2"] = 0.0
            hdr["PC2_1"] = 0.0
            hdr["CROTA"] = 0.0

        if 'CROTA' not in hdr:
            s = - np.sign(hdr["PC1_2"]) + (hdr["PC1_2"] == 0)
            hdr["CROTA"] = s * np.rad2deg(np.arccos(hdr["PC1_1"]))

    def _find_best_header_parameters(self):

        self.crval1_ref = self.hdr_small['CRVAL1']
        self.crval2_ref = self.hdr_small['CRVAL2']
        self.use_crota = True

        if 'CROTA' in self.hdr_small:
            self.crota_ref = self.hdr_small['CROTA']
        elif 'CROTA2' in self.hdr_small:
            self.crota_ref = self.hdr_small['CROTA2']
        else:
            s = - np.sign(self.hdr_small['PC1_2']) + (self.hdr_small['PC1_2'] == 0)
            self.crota_ref = np.rad2deg(np.arccos(self.hdr_small['PC1_1'])) * s
            self.hdr_small["CROTA"] = np.rad2deg(np.arccos(self.hdr_small['PC1_1']))
            # self.use_crota = False
        self.cdelta1_ref = self.hdr_small['CDELT1']
        self.cdelta2_ref = self.hdr_small['CDELT2']

        self.unit1 = self.hdr_small["CUNIT1"]
        self.unit2 = self.hdr_small["CUNIT2"]

        if "arcsec" in self.unit1:
            self.unit_lag = "arcsec"

        elif "deg" in self.unit1:
            warnings.warn("Units of headers in deg: Modyfying inputs units to deg.")
            self.lag_crval1 = Util.AlignCommonUtil.ang2pipi(u.Quantity(self.lag_crval1, "arcsec")).to("deg").value
            self.lag_crval2 = Util.AlignCommonUtil.ang2pipi(u.Quantity(self.lag_crval2, "arcsec")).to("deg").value
            self.lag_cdelta1 = Util.AlignCommonUtil.ang2pipi(u.Quantity(self.lag_cdelta1, "arcsec")).to("deg").value
            self.lag_cdelta2 = Util.AlignCommonUtil.ang2pipi(u.Quantity(self.lag_cdelta2, "arcsec")).to("deg").value
            self.unit_lag = "deg"
        if self.lag_solar_r is None:
            self.lag_solar_r = np.array([1.004])

        for lag in [self.lag_crval1, self.lag_crval2, self.lag_cdelta1, self.lag_cdelta2, self.lag_crota]:
            if lag is None:
                lag = np.array([0])


        if self.parallelism:
            results = np.zeros(
                (len(self.lag_crval1), len(self.lag_crval2), len(self.lag_cdelta1), len(self.lag_cdelta2),
                 len(self.lag_crota), len(self.lag_solar_r)), dtype="float")

            shmm_correlation, data_correlation = Util.MpUtils.gen_shmm(create=True, ndarray=results)
            self._correlation = {"name": shmm_correlation.name, "size": data_correlation.size,
                                 "shape": data_correlation.shape}
            del results
            for kk, d_solar_r in enumerate(self.lag_solar_r):
                Processes = []

                if self.coordinate_frame == "carrington":
                    self.data_large = self.function_to_apply(d_solar_r=d_solar_r, data=self.data_large,
                                                             hdr=self.hdr_large)
                elif self.coordinate_frame == "helioprojective":
                    self.data_large = self._create_submap_of_large_data(data_large=self.data_large)

                shmm_large, data_large = Util.MpUtils.gen_shmm(create=True, ndarray=copy.deepcopy(self.data_large))
                self._large = {"name": shmm_large.name, "dtype": data_large.dtype, "shape": data_large.shape}
                

                shmm_small, data_small = Util.MpUtils.gen_shmm(create=True, ndarray=copy.deepcopy(self.data_small))
                self._small = {"name": shmm_small.name, "dtype": data_small.dtype, "shape": data_small.shape}
                import matplotlib.pyplot as plt
                del self.data_large
                del self.data_small
                error_queue = Queue()
                for ii, d_cdelta1 in enumerate(self.lag_cdelta1):
                    for ll, d_cdelta2 in enumerate(self.lag_cdelta2):
                        for jj, d_crota in enumerate(self.lag_crota):
                            for ff, d_crval1 in enumerate(self.lag_crval1):
                                kwargs = {
                                    "d_crval1": d_crval1,
                                    "d_cdelta1": d_cdelta1,
                                    "d_cdelta2": d_cdelta2,
                                    "d_crota": d_crota,
                                    "d_solar_r": d_solar_r,
                                    "method": self.method,
                                    "lock": self.lock,
                                    "position": (ff, ii, ll, jj, kk),

                                }

                                Processes.append(Process(target=self._iteration_step_along_crval2, kwargs={**kwargs,'error_queue': error_queue}))

                if self.counts is None:
                    self.counts = mp.cpu_count()

                lenp = len(Processes)
                ii = -1
                is_close = []
                while (ii < lenp - 1):
                    ii += 1
                    Processes[ii].start()
                    while (np.sum([p.is_alive() for mm, p in zip(range(lenp), Processes) if
                                   (mm not in is_close)]) > self.counts):
                        pass
                    for kk, P in zip(range(lenp), Processes):
                        if kk not in is_close:
                            if (not (P.is_alive())) and (kk <= ii):
                                P.close()
                                is_close.append(kk)

                while (np.sum([p.is_alive() for mm, p in zip(range(lenp), Processes) if (mm not in is_close)]) != 0):
                    pass
                for kk, P in zip(range(lenp), Processes):
                    if kk not in is_close:
                        if (not (P.is_alive())) and (kk <= ii):
                            P.close()
                            is_close.append(kk)
            # Check for errors in the error queue
            while not error_queue.empty():
                error, traceback_str = error_queue.get()
                print(f"Error: {error}\nTraceback: {traceback_str}")
            shmm_correlation, data_correlation = Util.MpUtils.gen_shmm(create=False, **self._correlation)
            
            shmm_large, data_large = Util.MpUtils.gen_shmm(create=False, **self._large)
            shmm_small, data_small = Util.MpUtils.gen_shmm(create=False, **self._small)

            data_correlation_cp = copy.deepcopy(data_correlation)
            shmm_correlation.close()
            shmm_large.close()
            shmm_large.unlink()
            shmm_small.close()
            shmm_small.unlink()
            shmm_correlation.unlink()
        else:
            data_correlation_cp = np.zeros(
                (len(self.lag_crval1), len(self.lag_crval2), len(self.lag_cdelta1), len(self.lag_cdelta2),
                 len(self.lag_crota), len(self.lag_solar_r)), dtype="float")
            for hh, d_solar_r in enumerate(self.lag_solar_r):
                if self.coordinate_frame == "carrington":
                    self.data_large = self.function_to_apply(d_solar_r=d_solar_r, data=self.data_large,
                                                             hdr=self.hdr_large)
                elif self.coordinate_frame == "helioprojective":
                    self.data_large = self._create_submap_of_large_data(data_large=self.data_large)

                # shmm_large, data_large = Util.MpUtils.gen_shmm(create=True, ndarray=self.data_large)
                # self._large = {"name": shmm_large.name, "dtype": data_large.dtype, "shape": data_large.shape}
                # self.data_large = None
                #
                # shmm_small, data_small = Util.MpUtils.gen_shmm(create=True, ndarray=self.data_small)
                # self._small = {"name": shmm_small.name, "dtype": data_small.dtype, "shape": data_small.shape}
                # self.data_small = None
                #
                # shmm_large.close()
                # shmm_small.close()
                for ii, d_crval1 in enumerate(self.lag_crval1):
                    for jj, d_crval2 in enumerate(tqdm(self.lag_crval2)):
                        for kk, d_cdelta1 in enumerate(self.lag_cdelta1):
                            for mm, d_cdelta2 in enumerate(self.lag_cdelta2):
                                for ll, d_crota in enumerate(self.lag_crota):
                                    data_correlation_cp[ii, jj, kk, mm, ll, hh] = self._step_no_shmm(d_crval2=d_crval2,
                                                                                                  d_crval1=d_crval1,
                                                                                                  d_cdelta1=d_cdelta1,
                                                                                                  d_cdelta2=d_cdelta2,
                                                                                                  d_crota=d_crota,
                                                                                                  method=self.method,
                                                                                                  d_solar_r=d_solar_r,

                                                                                                  )
        self.data_correlation  =data_correlation_cp
        return data_correlation_cp

    def _carrington_transform(self, d_solar_r, data, hdr):

        spherical = rectify.CarringtonTransform(hdr, radius_correction=d_solar_r,
                                                reference_date=self.reference_date,
                                                rate_wave=self.rat_wave[
                                                    '%i' % (self.hdr_large['WAVELNTH'])])
        spherizer = rectify.Rectifier(spherical)
        image = spherizer(data, self.shape, self.lonlims, self.latlims, opencv=False, order=self.order, fill=-32762)
        image = np.where(image == -32762, np.nan, image)
        if Fits.HeaderDiff(hdr, self.hdr_large).identical:
            if self.path_save_figure is not None:
                plot.PlotFunctions.plot_fov(image, show=False,
                                            path_save='%s/image_large.pdf' % (self.path_save_figure))
                spherical = rectify.CarringtonTransform(self.hdr_small, radius_correction=d_solar_r,
                                                        reference_date=self.reference_date,
                                                        rate_wave=self.rat_wave[
                                                            '%i' % (self.hdr_large['WAVELNTH'])])
                spherizer = rectify.Rectifier(spherical)

                image_small = spherizer(self.data_small, self.shape, self.lonlims, self.latlims, opencv=False,
                                        order=self.order, fill=-32762)
                image_small = np.where(image_small == -32762, np.nan, image_small)

                plot.PlotFunctions.plot_fov(image_small, show=False,
                                            path_save='%s/image_small.pdf' % (self.path_save_figure))

        return image

    def _create_submap_of_large_data(self, data_large):
        if self.path_save_figure is not None:
            plot.PlotFunctions.simple_plot(self.hdr_large, data_large, show=False,
                                           path_save='%s/large_fov_before_cut.pdf' % (self.path_save_figure))

        hdr_cut = self.hdr_small.copy()
        w_xy_large = WCS(self.hdr_large.copy())

        if self.use_sunpy:
            w_cut = WCS(hdr_cut)
            idx_lon = np.where(np.array(w_cut.wcs.ctype, dtype="str") == "HPLN-TAN")[0][0]
            idx_lat = np.where(np.array(w_cut.wcs.ctype, dtype="str") == "HPLT-TAN")[0][0]
            x, y = np.meshgrid(np.arange(w_cut.pixel_shape[idx_lon]),
                               np.arange(w_cut.pixel_shape[idx_lat]), )  # t dépend de x,
            
            if w_cut.naxis == 2:
                coords_cut = w_cut.pixel_to_world(x, y)
            elif w_cut.naxis == 3:
                coords_cut,time = w_cut.pixel_to_world(x, y,0)
            else: raise Exception('Number of axis for the wcs object is unknown')
                

            longitude_cut = Util.AlignCommonUtil.ang2pipi(coords_cut.Tx)
            latitude_cut = Util.AlignCommonUtil.ang2pipi(coords_cut.Ty)
            coords_cut = SkyCoord(longitude_cut, latitude_cut, frame=coords_cut.frame)
            x_cut, y_cut = w_xy_large.world_to_pixel(coords_cut)


        else:
            longitude_cut, latitude_cut, dsun_obs_cut = Util.AlignEUIUtil.extract_EUI_coordinates(hdr_cut)
            x_cut, y_cut = w_xy_large.world_to_pixel(longitude_cut, latitude_cut)
        image_large_cut = Util.AlignCommonUtil.interpol2d(np.array(data_large, dtype=np.float64), x=x_cut, y=y_cut,
                                                          order=self.order, fill=-32768)
        # breakpoint()
        # image_large_cut_ = Util.AlignCommonUtil.interpol2d(np.array(data_large, dtype=np.float64), x=x_cut_, y=y_cut_,order=1,fill=-32768)

        image_large_cut[image_large_cut == -32768] = np.nan
        self.hdr_large = hdr_cut.copy()
        w_xy_small = WCS(self.hdr_small.copy())
        if self.use_sunpy:
            # coords_cut_small = SkyCoord(longitude_cut, latitude_cut, frame=coords_cut.frame)
            if w_cut.naxis == 2:
                x_cut, y_cut = w_xy_small.world_to_pixel(coords_cut)
            elif w_cut.naxis == 3:
                x_cut, y_cut, time_ = w_xy_small.world_to_pixel(coords_cut,time)
            else: raise Exception('Number of axis for the wcs object is unknown')

        else:
            x_cut, y_cut = w_xy_small.world_to_pixel(longitude_cut, latitude_cut)

        image_small_cut = Util.AlignCommonUtil.interpol2d(np.array(self.data_small.copy(), dtype=np.float64), x=x_cut,
                                                          y=y_cut, order=self.order, fill=-32768)
        image_small_cut[image_small_cut == -32768] = np.nan

        self.data_small = image_small_cut
        self.hdr_small = hdr_cut.copy()
        levels = [0.15 * np.nanmax(self.data_small)]

        if self.path_save_figure is not None:
            date_small = self.hdr_small["DATE-AVG"]
            date_small = date_small.replace(":", "_")
            plot.PlotFunctions.simple_plot(self.hdr_large, image_large_cut, show=False,
                                           path_save='%s/large_fov_%s.pdf' % (self.path_save_figure, date_small))
            plot.PlotFunctions.simple_plot(self.hdr_small, self.data_small, show=False,
                                           path_save='%s/small_fov_%s.pdf' % (self.path_save_figure, date_small))
            plot.PlotFunctions.contour_plot(self.hdr_large, image_large_cut, self.hdr_small, self.data_small,
                                            show=False, path_save='%s/compare_plot_%s.pdf' % (self.path_save_figure,
                                                                                              date_small),
                                            levels=levels)
        self.step_figure = False
        return np.array(image_large_cut)

    def _interpolate_on_large_data_grid(self, d_solar_r, data, hdr):

        w_xy_small = WCS(hdr)
        longitude_large, latitude_large, dsun_obs_large = Util.AlignEUIUtil.extract_EUI_coordinates(self.hdr_large)

        use_sunpy = False
        for mapping in [WCS_FRAME_MAPPINGS, FRAME_WCS_MAPPINGS]:
            if mapping[-1][0].__module__ == 'sunpy.coordinates.wcs_utils':
                use_sunpy = True
        if use_sunpy:

            w_large = WCS(self.hdr_large)
            idx_lon = np.where(np.array(w_large.wcs.ctype, dtype="str") == "HPLN-TAN")[0][0]
            idx_lat = np.where(np.array(w_large.wcs.ctype, dtype="str") == "HPLT-TAN")[0][0]
            x, y = np.meshgrid(np.arange(w_large.pixel_shape[idx_lon]),
                               np.arange(w_large.pixel_shape[idx_lat]), )  # t dépend de x,
            if w_large.naxis == 2:
                coords = w_large.pixel_to_world(x, y)
            elif w_large.naxis == 3:
                coords,time = w_large.pixel_to_world(x, y,0)
            else:
                raise Exception('Number of axis for the wcs object is unknown')
            
            if w_large.naxis == 2:
                x_large, y_large = w_xy_small.world_to_pixel(coords)
            elif w_large.naxis == 3:
                time_matrix = np.empty(coords.shape, dtype='datetime64[ns]')
                for i in range(coords.shape[0]):
                    for j in range(coords.shape[1]):
                        time_matrix[i, j] =  np.datetime64(str(coords.obstime))
                time_matrix =  Time(time_matrix)
                x_large, y_large, z = w_xy_small.world_to_pixel(coords,time_matrix)
            else:
                raise Exception('Number of axis for the wcs object is unknown,')
        else:
            if w_xy_small.naxis == 2:
                x_large, y_large = w_xy_small.world_to_pixel(longitude_large, latitude_large)
            elif w_xy_small.naxis == 3:
                time_matrix = np.empty(longitude_large.shape, dtype='datetime64[ns]')
                for i in range(longitude_large.shape[0]):
                    for j in range(longitude_large.shape[1]):
                        time_matrix[i, j] =  np.datetime64(str(w_xy_small.wcs.dateobs))
                # raise Exception(f'w_xy_small.wcs.dateobs {w_xy_small.wcs.dateobs,type(w_xy_small.wcs.dateobs),str(w_xy_small.wcs.dateobs)}')
                time_matrix =  Time(time_matrix)
                x_large, y_large, time = w_xy_small.world_to_pixel(longitude_large, latitude_large,time_matrix)
                
        image_small_shft = Util.AlignCommonUtil.interpol2d(np.array(copy.deepcopy(data), dtype=np.float64),
                                                           x=x_large, y=y_large, order=self.order,
                                                           fill=-32768)
        image_small_shft = np.where(image_small_shft == -32768, np.nan, image_small_shft)

        return image_small_shft

    @staticmethod
    def _get_naxis(hdr):
        if "ZNAXIS1" in hdr:
            naxis1 = hdr["ZNAXIS1"]
            naxis2 = hdr["ZNAXIS2"]
        else:
            naxis1 = hdr["NAXIS1"]
            naxis2 = hdr["NAXIS2"]
        return naxis1, naxis2
