import argparse
import datetime
import glob
import os
import re
import math

from tqdm import tqdm

import pandas as pd

from textwrap import dedent


def combineDelayFiles(outName, loc=os.getcwd(), source = 'model', ext='.csv'):
    files = glob.glob(os.path.join(loc, '*' + ext))

    if source == 'model':
        print('Ensuring that "Datetime" column exists in files')
        addDateTimeToFiles(files)

    # If single file, just copy source
    if len(files) == 1:
        import shutil
        shutil.copy(files[0], outName)
        return

    print('Combining {} delay files'.format(source))
    if source == 'model':
        concatDelayFiles(
            files,
            sort_list=['ID', 'Datetime'],
            outName=outName
        )
    else:
        concatDelayFiles(
            files,
            sort_list=['ID', 'Date'],
            outName=outName
        )


def addDateTimeToFiles(fileList, force=False):
    ''' Run through a list of files and add the datetime of each file as a column '''

    print('Adding Datetime to delay files')

    for f in tqdm(fileList):
        data = pd.read_csv(f)

        if 'Datetime' in data.columns and not force:
            print(
                'File {} already has a "Datetime" column, pass'
                '"force = True" if you want to override and '
                're-process'.format(f)
            )
        else:
            try:
                dt = getDateTime(f)
                data['Datetime'] = dt
                # drop all lines with nans
                data.dropna(how='any', inplace=True)
                # drop all duplicate lines
                data.drop_duplicates(inplace=True)
                data.to_csv(f, index=False)
            except (AttributeError, ValueError):
                print(
                    'File {} does not contain datetime info, skipping'
                    .format(f)
                )
        del data


def getDateTime(filename):
    ''' Parse a datetime from a RAiDER delay filename '''
    filename = os.path.basename(filename)
    dtr = re.compile(r'\d{8}T\d{6}')
    dt = dtr.search(filename)
    return datetime.datetime.strptime(
            dt.group(), 
            '%Y%m%dT%H%M%S'
        )

def lon_distance(origin, destination):
    """
    Source: https://stackoverflow.com/questions/19412462/getting-distance-between-two-points-based-on-latitude-longitude
    Calculate the Haversine distance.

    Parameters
    ----------
    origin : tuple of float
        (lat, long)
    destination : tuple of float
        (lat, long)

    Returns
    -------
    distance_in_km : float

    Examples
    --------
    >>> origin = (48.1372, 11.5756)  # Munich
    >>> destination = (52.5186, 13.4083)  # Berlin
    >>> round(distance(origin, destination), 1)
    504.2
    """
    lat1, lon1 = origin
    lat2, lon2 = destination
    radius = 6371  # km

    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) * math.sin(dlat / 2) +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) * math.sin(dlon / 2))
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    d = radius * c

    return d


def concatDelayFiles(
    fileList,
    sort_list=['ID', 'Datetime'],
    return_df=False,
    outName=None
):
    ''' 
    Read a list of .csv files containing the same columns and append them 
    together, sorting by specified columns 
    '''
    dfList = []

    print('Concatenating delay files')

    for f in tqdm(fileList):
        dfList.append(pd.read_csv(f))

    df_c = pd.concat(
        dfList,
        ignore_index=True
    ).drop_duplicates().reset_index(drop=True)
    df_c.sort_values(by=sort_list, inplace=True)

    print('Total number of rows in the concatenated file: {}'.format(df_c.shape[0]))
    print('Total number of rows containing NaNs: {}'.format(
            df_c[df_c.isna().any(axis=1)].shape[0]
        )
    )

    if return_df or outName is None:
        return df_c
    else:
        # drop all lines with nans
        df_c.dropna(how='any', inplace=True)
        # drop all duplicate lines
        df_c.drop_duplicates(inplace=True)
        df_c.to_csv(outName, index=False)


def mergeDelayFiles(
        raiderFile, 
        ztdFile, 
        col_name='ZTD', 
        raider_delay='totalDelay', 
        outName=None,
        localTime=None
    ):
    '''
    Merge a combined RAiDER delays file with a GPS ZTD delay file
    '''
    print('Merging delay files {} and {}'.format(raiderFile, ztdFile))

    dfr = pd.read_csv(raiderFile, parse_dates=['Datetime'])
    dfz = readZTDFile(ztdFile, col_name=col_name)

    # If specified, convert to local-time reference frame WRT 0 longitude
    if localTime is not None:
        from RAiDER.getStationDelays import seconds_of_day
        import numpy as np

        localTime_hrs = seconds_of_day(localTime.split(' ')[0])/3600
        localTime_hrthreshold = int(localTime.split(' ')[1])
        #speed at lat, assuming 1669.8 km/h rotation rate and circumference at equator
        dfr['Rot_rate'] = np.cos(np.deg2rad(dfr['Lat'])) * 1669.8
        dfz['Rot_rate'] = np.cos(np.deg2rad(dfz['Lat'])) * 1669.8
        #distance to 0 longitude, assuming 40075.1600832 km circumference at equator
        #make sure to convert longitude from -180/180 to 0/360 convention
        dfr['dist_to_0lon'] = lon_distance((0,dfr['Lon'] % 360),(dfr['Lat'], dfr['Lon'] % 360))
        dfz['dist_to_0lon'] = lon_distance((0,dfz['Lon'] % 360),(dfz['Lat'], dfz['Lon'] % 360))
        #from speed and distance estimates above, estimate desired --localtime WRT user input
        dfr['Localtime'] = ((dfr['dist_to_0lon'] / dfr['Rot_rate']) + localTime_hrs) * 3600
        print("dfr['Rot_rate']",dfr['Rot_rate'])
        print("dfr['dist_to_0lon']",dfr['dist_to_0lon'])
        print("dfr['Localtime']",dfr['Localtime'])
        print("(dfr['Localtime'] // 3600).astype(int)",(dfr['Localtime'] // 3600).astype(int))
        dfr['Localtime'] = dfr['Datetime'].apply(lambda dt: dt.replace(hour=(dfr['Localtime'] // 3600).astype(int), 
                          minute=(dfr['Localtime'] % 3600 / 60.0).astype(int), second=(dfr['Localtime'] % 60).astype(int)))
        dfz['Localtime'] = ((dfz['dist_to_0lon'] / dfz['Rot_rate']) + localTime_hrs) * 3600
        dfz['Localtime'] = dfz['Datetime'].apply(lambda dt: dt.replace(hour=(dfz['Localtime'] // 3600).astype(int), 
                          minute=(dfz['Localtime'] % 3600 / 60.0).astype(int), second=(dfz['Localtime'] % 60).astype(int)))

        #filter out data outside of --localtime hour threshold
        dfz['Localtime_l'] = dfz['Localtime'] - datetime.timedelta(hours=localTime_hrthreshold)
        dfz['Localtime_u'] = dfz['Localtime'] + datetime.timedelta(hours=localTime_hrthreshold)
        dfz = dfz[(dfz['Datetime'] >= dfz['Localtime_l']) & (dfz['Datetime'] <= dfz['Localtime_u'])]
        print('Total number of datapoints dropped in {} for not being within {} hrs of specified local-time {}: {} out of {}'.format(
               ztdFile, localTime.split('')[1], localTime.split('')[0], dfz[dfz.isna().any(axis=1)].shape[0], dfz.shape[0]))
        dfr['Localtime_l'] = dfr['Localtime'] - datetime.timedelta(hours=localTime_hrthreshold)
        dfr['Localtime_u'] = dfr['Localtime'] + datetime.timedelta(hours=localTime_hrthreshold)
        dfr = dfr[(dfr['Datetime'] >= dfr['Localtime_l']) & (dfr['Datetime'] <= dfr['Localtime_u'])]
        print('Total number of datapoints dropped in {} for not being within {} hrs of specified local-time {}: {} out of {}'.format(
               ztdFile, localTime.split('')[1], localTime.split('')[0], dfr[dfr.isna().any(axis=1)].shape[0], dfr.shape[0]))
        # drop all lines with nans
        dfz.dropna(how='any', inplace=True)
        dfr.dropna(how='any', inplace=True)
        # drop all duplicate lines
        dfz.drop_duplicates(inplace=True)
        dfr.drop_duplicates(inplace=True)
        #drop and rename columns
        dfz.drop(columns=['Rot_rate', 'dist_to_0lon', 'Localtime_l', 'Localtime_u', 'Datetime'], inplace=True)
        dfz.rename(columns={'Localtime': 'Datetime'}, inplace=True)
        dfr.drop(columns=['Rot_rate', 'dist_to_0lon', 'Localtime_l', 'Localtime_u', 'Datetime'], inplace=True)
        dfr.rename(columns={'Localtime': 'Datetime'}, inplace=True)

    print('Beginning merge')

    dfc = dfr.merge(
            dfz[['ID', 'Datetime', 'ZTD']], 
            how='left', 
            left_on=['Datetime', 'ID'], 
            right_on=['Datetime', 'ID'], 
            sort=True
        )
    dfc['ZTD_minus_RAiDER'] = dfc['ZTD'] - dfc[raider_delay]

    print('Total number of rows in the concatenated file: {}'.format(dfc.shape[0]))
    print('Total number of rows containing NaNs: {}'.format(
            dfc[dfc.isna().any(axis=1)].shape[0]
        )
    )
    print('Merge finished')

    if outName is None:
        return dfc
    else:
        # drop all lines with nans
        dfc.dropna(how='any', inplace=True)
        # drop all duplicate lines
        dfc.drop_duplicates(inplace=True)
        dfc.to_csv(outName, index=False)


def readZTDFile(filename, col_name='ZTD'):
    '''
    Read and parse a GPS zenith delay file
    '''
    try:
        data = pd.read_csv(filename, parse_dates=['Date'])
        times = data['times'].apply(lambda x: datetime.timedelta(seconds=x))
        data['Datetime'] = data['Date'] + times
    except KeyError:
        data = pd.read_csv(filename, parse_dates=['Datetime'])

    data.rename(columns={col_name: 'ZTD'}, inplace=True)
    return data


def create_parser():
    """Parse command line arguments using argparse."""
    p = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=dedent("""\
            Combine delay files from a weather model and GPS Zenith delays
            Usage examples:
            raiderCombine.py --raiderDir './*' --raider 'combined_raider_delays.csv'
            raiderCombine.py  --raiderDir ERA5/ --raider ERA5_combined_delays.csv --gnss UNRCombined_gnss.csv -o Combined_delays.csv 
            """)
    )

    p.add_argument(
        '--raider', dest='raider_file',
        help=dedent("""\
            .csv file containing RAiDER-derived Zenith Delays. 
            Should contain columns "ID" and "Datetime" in addition to the delay column
            If the file does not exist, I will attempt to create it from a directory of 
            delay files. 
            """),
        required=True
    )
    p.add_argument(
        '--raiderDir', '-d', dest='raider_folder',
        help=dedent("""\
            Directory containing RAiDER-derived Zenith Delay files.  
            Files should be named with a Datetime in the name and contain the 
            column "ID" as the delay column names.
            """),
        default=os.getcwd()
    )
    p.add_argument(
        '--gnssDir', '-gd', dest='gnss_folder',
        help=dedent("""\
            Directory containing GNSS-derived Zenith Delay files.  
            Files should contain the column "ID" as the delay column names 
            and times should be denoted by the "Date" key.
            """),
        default=os.getcwd()
    )

    p.add_argument(
        '--gnss', dest='gnss_file',
        help=dedent("""\
            Optional .csv file containing GPS Zenith Delays. Should contain columns "ID", "ZTD", and "Datetime"
            """),
        default=None
    )

    p.add_argument(
        '--raider_column',
        '-r',
        dest='raider_column_name',
        help=dedent("""\
            Name of the column containing RAiDER delays. Only used with the "--gnss" option
            """),
        default='totalDelay'
    )
    p.add_argument(
        '--column',
        '-c',
        dest='column_name',
        help=dedent("""\
            Name of the column containing GPS Zenith delays. Only used with the "--gnss" option

            """),
        default='ZTD'
    )

    p.add_argument(
        '--out',
        '-o',
        dest='out_name',
        help=dedent("""\
            Name to use for the combined delay file. Only used with the "--gnss" option

            """),
        default='Combined_delays.csv'
    )

    p.add_argument(
        '--localtime',
        '-lt',
        dest='local_time',
        help=dedent("""\
            "Optional control to pass only data at local-time WRT user-defined time at 0 longitude (1st argument),
             and within +/- specified hour threshold (2nd argument).
             By default UTC is passed as is without local-time conversions.
             Input in 'HH:MM:SS H', e.g. '16:00:00 1'"

            """),
        default=None
    )

    return p


def parseCMD():
    """
    Parse command-line arguments and pass to tropo_delay
    We'll parse arguments and call delay.py.
    """

    p = create_parser()
    args = p.parse_args()

    if ~os.path.exists(args.raider_file):
        combineDelayFiles(args.raider_file, loc=args.raider_folder)

    if ~os.path.exists(args.gnss_file):
        combineDelayFiles(args.gnss_file, source = 'GNSS', loc=args.gnss_folder)

    if args.gnss_file is not None:
        mergeDelayFiles(
            args.raider_file,
            args.gnss_file,
            col_name=args.column_name,
            raider_delay=args.raider_column_name,
            outName=args.out_name,
            localTime=args.local_time
        )
