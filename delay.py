"""Compute the delay from a point to the transmitter.

Dry and hydrostatic delays are calculated in separate functions.
Currently we take samples every _STEP meters, which causes either
inaccuracies or inefficiencies, and we no longer can integrate to
infinity.
"""


from osgeo import gdal
gdal.UseExceptions()

# standard imports
import itertools
import numpy as np
import os
import pyproj
#import queue
import threading
import sys


# local imports
import utils.constants as const
import utils.demdownload as dld
import utils.losreader as losreader
import utils.util as util
from utils.constants import Zenith

# Step in meters to use when integrating
_STEP = const._STEP
# Top of the troposphere
_ZREF = const._ZMAX


def _get_lengths(look_vecs):
    '''
    Returns the lengths of a vector or set of vectors
    '''
    if look_vecs is Zenith:
        return _ZREF

    lengths = np.linalg.norm(look_vecs, axis=-1)
    lengths[~np.isfinite(lengths)] = 0
    return lengths


def _getZenithLookVecs(lats, lons, heights, zref = _ZREF):

    '''
    Returns look vectors when Zenith is used
    '''
    return (np.array((util.cosd(lats)*util.cosd(lons),
                              util.cosd(lats)*util.sind(lons),
                              util.sind(lats))).T
                    * (zref - heights)[..., np.newaxis])


def _compute_ray(L, S, V, stepSize):
    '''
    Compute and return points along a ray, given a total length, 
    start position (in x,y,z) and a unit look vector.
    '''
    # Have to handle the case where there are invalid data
    try:
        thisspace = np.arange(0, L, stepSize)
    except ValueError:
        thisspace = np.array([])
    ray = S + thisspace[..., np.newaxis]*V
    return ray


def _helper(tup):
    return _compute_ray(tup[0], tup[1], tup[2], tup[3])
    #return _compute_ray(L, S, V, stepSize)

def _get_rays_p(lengths, stepSize, start_positions, scaled_look_vecs, Nproc = 4):
    import multiprocessing as mp

    # setup for multiprocessing
    data = zip(lengths, start_positions, scaled_look_vecs, [stepSize]*len(lengths))

    pool = mp.Pool(Nproc)
    positions_l = pool.map(helper, data)
    return positions_l


def _get_rays_d(lengths, stepSize, start_positions, scaled_look_vecs, Nproc = 2):
   import dask.bag as db
   L = db.from_sequence(lengths)
   S = db.from_sequence(start_positions)
   Sv = db.from_sequence(scaled_look_vecs)
   Ss = db.from_sequence([stepSize]*len(lengths))

   # setup for multiprocessing
   data = db.zip(L, S, Sv, Ss)

   positions_l = db.map(helper, data)
   return positions_l.compute()


def _get_rays(lengths, stepSize, start_positions, scaled_look_vecs):
    '''
    Create the integration points for each ray path. 
    ''' 
    positions_l= []
    rayData = zip(lengths, start_positions, scaled_look_vecs)
    for L, S, V in rayData:
        positions_l.append(_compute_ray(L, S, V, stepSize))

    return positions_l


def _transform(ray, oldProj, newProj):
    '''
    Transform a ray from one coordinate system to another
    '''
    newRay = np.stack(
                pyproj.transform(
                      oldProj, newProj, ray[:,0], ray[:,1], ray[:,2])
                      ,axis = -1)
    return newRay


def _re_project(tup): 
    newPnt = _transform(tup[0],tup[1], tup[2])
    return newPnt
def f(x):
    ecef = pyproj.Proj(proj='geocent')
    return _transform(x, ecef, newProj)

def getIntFcn(weatherObj, itype = 'wet', interpType = 'scipy'):
    '''
    Function to create and return an Interpolator object
    '''
    import interpolator as intprn

    ifFun = intprn.Interpolator()
    ifFun.setPoints(*weatherObj.getPoints())
    ifFun.setProjection(weatherObj.getProjection())

    if itype == 'wet':
        ifFun.getInterpFcns(weatherObj.getWetRefractivity(), interpType = interpType)
    elif itype == 'hydro':
        ifFun.getInterpFcns(weatherObj.getHydroRefractivity(), interpType = interpType)
    return ifFun
 

def _common_delay(weatherObj, lats, lons, heights, 
                  look_vecs, zref = None,
                  stepSize = _STEP, interpType = 'rgi',
                  verbose = False, nproc = 8, useDask = False):
    """
    This function calculates the line-of-sight vectors, estimates the point-wise refractivity
    index for each one, and then integrates to get the total delay in meters. The point-wise
    delay is calculated by interpolating the weatherObj, which contains a weather model with
    wet and hydrostatic refractivity at each weather model grid node, to the points along 
    the ray. The refractivity is integrated along the ray to get the final delay. 

    Inputs: 
     weatherObj - a weather model object
     lats       - Grid of latitudes for each ground point
     lons       - Grid of longitudes for each ground point
     heights    - Grid of heights for each ground point
     look_vecs  - Grid of look vectors (should be full-length) for each ground point
     raytrace   - If True, will use the raytracing method, if False, will use the Zenith 
                  + projection method
     stepSize   - Integration step size in meters 
     intpType   - Can be one of 'scipy': LinearNDInterpolator, or 'sane': _sane_interpolate. 
                  Any other string will use the RegularGridInterpolate method
     nproc      - Number of parallel processes to use if useDask is True
     useDask    - use Dask to parallelize ray calculation

    Outputs: 
     delays     - A list containing the wet and hydrostatic delays for each ground point in 
                  meters. 
    """

    # If the number of points to interpolate are low, don't parallelize
    if np.prod(lats.shape) < parThresh:
       useDask = False
       nproc = 1

    # Determine if/what type of parallization to use
    if useDask:
       import dask.bag as db
    elif nproc > 1:
       import multiprocessing as mp
    else:
       pass

    if zref is None:
       zref = _ZREF

    # If weather model nodes only are desired, the calculation is very quick
    if look_vecs is Zenith:
        _,_,zs = weatherObj.getPoints()
        look_vecs = _getZenithLookVecs(lats, lons, heights, zref = zref)
        wet_pw  = weatherObj.getWetRefractivity()
        hydro_pw= weatherObj.getHydroRefractivity()
        wet_delays = _integrateZenith(zs, wet_pw)
        hydro_delays = _integrateZenith(zs, hydro_pw)
        return wet_delays,hydro_delays

    if verbose:
        import time
        print('_common_delay: Starting look vector calculation')
        print('_common_delay: The integration stepsize is {} m'.format(stepSize))
        st = time.time()

    # Otherwise, set off on the interpolation road
    mask = np.isnan(heights)

    # Get the integration points along the look vectors
    # First get the length of each look vector, get integration steps along 
    # each, then get the unit vector pointing in the same direction
    lengths = _get_lengths(look_vecs)
    lengths[mask] = np.nan
    start_positions = np.array(util.lla2ecef(lats, lons, heights)).T
    scaled_look_vecs = look_vecs / lengths[..., np.newaxis]
    positions_l= _get_rays(lengths, stepSize, start_positions, scaled_look_vecs)

    if verbose:
        print('_common_delay: Finished _get_rays')
        ft = time.time()
        print('Ray initialization took {:4.2f} secs'.format(ft-st))
        print('_common_delay: Starting _re_project')
        st = time.time()

    ecef = pyproj.Proj(proj='geocent')
    newProj = weatherObj.getProjection()
    if useDask:
        if verbose:
            print('Beginning re-projection using Dask')
        Npart = min(len(positions_l)//100 + 1, 1000)
        bag = [(pos, ecef, newProj) for pos in positions_l]
        PntBag = db.from_sequence(bag, npartitions=Npart)
        newPts = PntBag.map(_re_project).compute()
    else:
        if verbose:
            print('Beginning re-projection without Dask')
        newPts = list(map(f, positions_l))

    newPts = [np.vstack([p[:,1], p[:,0], p[:,2]]).T for p in newPts]

    if verbose:
        print('_common_delay: Finished re-projecting')
        print('_common_delay: The size of look_vecs is {}'.format(np.shape(look_vecs)))
        ft = time.time()
        print('Re-projecting took {:4.2f} secs'.format(ft-st))
        print('_common_delay: Starting Interpolation')
        st = time.time()

    # Define the interpolator objects
    ifWet = getIntFcn(weatherObj,interpType =interpType)
    ifHydro = getIntFcn(weatherObj,itype = 'hydro', interpType = interpType)

    # Depending on parallelization, do the interpolation
    if useDask:
        if verbose:
            print('Beginning interpolation using Dask')
        Npart = min(len(newPts)//100 + 1, 1000)
        PntBag = db.from_sequence(newPts, npartitions=Npart)
        wet_pw = PntBag.map(interpRay).compute()
        hydro_pw = PntBag.map(interpRay).compute()
    elif nproc > 1:
        if verbose:
            print('Beginning interpolation without Dask')
        import multiprocessing as mp
        pool = mp.Pool(12)
        inp1 = zip([ifWet]*len(newPts), newPts)
        inp2 = zip([ifHydro]*len(newPts), newPts)

        wet_pw = pool.map(interpRay,inp1)
        hydro_pw = pool.map(interpRay, inp2)
    else:
        wet_pw, hydro_pw = [], []
        count = 0
        for pnt in newPts:
            wet_pw.append(interpRay((ifWet, pnt)))
            hydro_pw.append(interpRay((ifHydro, pnt)))
            count = count+1
       
  
    if verbose:
        print('_common_delay: Finished interpolation')
        ft = time.time()
        print('Interpolation took {:4.2f} secs'.format(ft-st))
        print('Average of {:1.6f} secs/ray'.format(.5*(ft-st)/len(newPts)))
        print('_common_delay: finished point-wise delay calculations')

    # intergrate the point-wise delays to get total slant delay
    delays = _integrateLOS(stepSize, wet_pw, hydro_pw)

    return delays


# call the interpolator on each ray
def interpRay(tup):
    fcn, ray = tup
    return fcn(ray)[0]


def _integrateLOS(stepSize, wet_pw, hydro_pw):
    delays = [] 
    for d in (wet_pw, hydro_pw):
        delays.append(_integrate_delays(stepSize, d))
    return delays


def _integrateZenith(zs, pw):
    return 1e-6*np.trapz(pw, zs, axis = 2)


def _integrate_delays(stepSize, refr):
    '''
    This function gets the actual delays by integrating the refractivity in 
    each node. Refractivity is given in the 'refr' variable. 
    '''
    delays = []
    for ray in refr:
        delays.append(int_fcn(ray, stepSize))
    return delays


# integrate the delays to get overall delay
def int_fcn(y, dx):
    return 1e-6*dx*np.nansum(y)


#TODO: the following three fcns are unclear if/how they are needed. 
# likely need to see how they work with tropo_delay
def delay_over_area(weather, 
                    lat_min, lat_max, lat_res, 
                    lon_min, lon_max, lon_res, 
                    ht_min, ht_max, ht_res, 
                    los=Zenith, 
                    parallel = True, verbose = False):
    """Calculate (in parallel) the delays over an area."""
    lats = np.arange(lat_min, lat_max, lat_res)
    lons = np.arange(lon_min, lon_max, lon_res)
    hts = np.arange(ht_min, ht_max, ht_res)

    if verbose:
        print('delay_over_area: Size of lats: {}'.format(np.shape(lats)))
        print('delay_over_area: Size of lons: {}'.format(np.shape(lons)))
        print('delay_over_area: Size of hts: {}'.format(np.shape(hts)))

    # It's the cartesian product (thanks StackOverflow)
    llas = np.array(np.meshgrid(lats, lons, hts)).T.reshape(-1, 3)
    if verbose:
        print('delay_over_area: Size of llas: {}'.format(np.shape(llas)))

    if verbose:
        print('delay_over_area: running delay_from_grid')

    return delay_from_grid(weather, llas, los, parallel=parallel, verbose = verbose)


def delay_from_files(weather, lat, lon, ht, zref = None, parallel=False, los=Zenith,
                     raytrace=True, verbose = False):
    """
    Read location information from files and calculate delay.
    """
    if zref is None:
       zref = _ZREF

    lats = util.gdal_open(lat)
    lons = util.gdal_open(lon)
    hts = util.gdal_open(ht)

    if los is not Zenith:
        incidence, heading = util.gdal_open(los)
        if raytrace:
            los = losreader.los_to_lv(
                incidence, heading, lats, lons, hts, zref).reshape(-1, 3)
        else:
            los = incidence.flatten()

    # We need the three to be the same shape so that we know what to
    # reshape hydro and wet to. Plus, them being different sizes
    # indicates a definite user error.
    if not lats.shape == lons.shape == hts.shape:
        raise ValueError('lat, lon, and ht should have the same shape, but ' + 
                         'instead lat had shape {}, lon had shape '.format(lats.shape) + 
                         '{}, and ht had shape {}'.format(lons.shape,hts.shape))

    llas = np.stack((lats.flatten(), lons.flatten(), hts.flatten()), axis=1)
    hydro, wet = delay_from_grid(weather, llas, los,
                                 parallel=parallel, raytrace=raytrace, verbose = verbose)
    hydro, wet = np.stack((hydro, wet)).reshape((2,) + lats.shape)
    return hydro, wet


def get_weather_and_nodes(model, filename, zmin=None):
    """Look up weather information from a model and file.

    We use the module.load method to load the weather model file, but
    we'll also create a weather model object for it.
    """
    # TODO: Need to check how this fcn will fit into the new framework
    xs, ys, proj, t, q, z, lnsp = model.load(filename)
    return (reader.read_model_level(module, xs, ys, proj, t, q, z, lnsp, zmin),
            xs, ys, proj)


def tropo_delay(time, 
                los = None, lat = None, lon = None, heights = None, 
                weather = None, zref = 15000, out = None, 
                parallel=True,verbose = False, download_only = False):
    """Calculate troposphere delay from command-line arguments.

    We do a little bit of preprocessing, then call
    _tropo_delay_with_values. Then we'll write the output to the output
    file.
    """
    from models.allowed import checkIfImplemented
    from datetime import datetime as dt

    if verbose:
        print('type of time: {}'.format(type(time)))
        print('Download-only is {}'.format(download_only))

    if out is None:
        out = os.getcwd()

    # ensure inputs are numpy arrays or None
    [lats, lons, los, hgts] = util.enforceNumpyArray(lat, lon, los, heights)

    # Make weather
    weather_model, weather_files, weather_model_name = \
               weather['type'],weather['files'],weather['name']
    checkIfImplemented(weather_model_name)
    util.check4LatLon(weather_files, lats)

    # check whether weather model files are supplied
    if weather_files is None:
       download_flag, f = downloadWMFile(weather_model.Model(), time, outLoc, verbose)

    # if no weather model files supplied, check the standard location
    if download_flag:
        try:
           weather_model.fetch(lats, lons, time, f)
        except Exception as e:
           print('ERROR: Unable to download weather data')
           print('Exception encountered: {}'.format(e))
           sys.exit(0)
 
        # exit on download if download_only requested
        if download_only:
            print('WARNING: download_only flag selected. I will only '\
                  'download the weather model, '\
                  ' without doing any further processing.')
            return None, None


    # Load the weather model data
    if weather_files is not None:
       weather_model.load(*weather_files)
       download_flag = False
    elif weather_model_name == 'pickle':
        weather = util.pickle_load(weather_files)
    else:
        # output file for storing the weather model
        weather_model.load(f)
    if verbose:
        print(weather)
        #p = weather.plot(p)


    # Pull the lat/lon data if using the weather model 
    if lats is None:
       lats,lons = weather.getLL() 
       lla = weather.getProjection()
       util.writeLL(lats, lons,lla, weather_model_name, out)


    # LOS check and load
    util.checkShapes(los, lats, lons, hgts)
    util.checkLOS(los, raytrace, np.prod(lats.shape))

    # Save the shape so we can restore later, but flatten to make it
    # easier to think about
    llas = np.stack((lats, lons, hgts), axis=-1)
    real_shape = llas.shape[:-1]
    llas = llas.reshape(-1, 3)
    lats, lons, hgts = np.moveaxis(llas, -1, 0)

    # Call _common_delay to compute the hydrostatic and wet delays
    if parallel:
       useDask = True
       nproc = 16
    else:
       nproc = 1
    wet, hydro = _common_delay(weather, lats, lons, hgts, los, zref = zref,\
                  nproc = nproc, useDask = useDask, verbose = verbose)

    # Restore shape
    try:
        hydro, wet = np.stack((hydro, wet)).reshape((2,) + real_shape)
    except:
        pass

    return wet, hydro

    
