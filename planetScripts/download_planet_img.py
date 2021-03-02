#!/usr/bin/env python
# Filename: download_planet_img 
"""
introduction:

authors: Huang Lingcao
email:huanglingcao@gmail.com
add time: 05 October, 2019
"""

import sys,os
from optparse import OptionParser

HOME = os.path.expanduser('~')

# path of DeeplabforRS
codes_dir2 = HOME + '/codes/PycharmProjects/DeeplabforRS'
sys.path.insert(0, codes_dir2)

import basic_src.io_function as io_function
import basic_src.basic as basic
import vector_gpd
import basic_src.map_projection as map_projection

# import thest two to make sure load GEOS dll before using shapely
import shapely
from shapely.geometry import mapping # transform to GeJSON format
import geopandas as gpd
from shapely.geometry import shape

from datetime import datetime
import json
import time
import random

import multiprocessing
from multiprocessing import Pool
from multiprocessing import Process


from retrying import retry

from planet import api
from planet.api.exceptions import APIException
from planet.api import filters
# ClientV1 provides basic low-level access to Planet’s API. Only one ClientV1 should be in existence for an application.
client = None # api.ClientV1(api_key="abcdef0123456789")  #

# more on the asset type are available at: https://developers.planet.com/docs/data/psscene4band/

# asset_types=['analytic_sr','analytic_xml','udm'] #   # surface reflectance, metadata, mask file
# For color corrected 3 band data
asset_types=['visual', 'visual_xml', 'udm']
# if analytic_sr not available, we will download analytic (supplementary asset types)
supp_asset_types = ['analytic']

downloaded_scene_geometry = []       # the geometry (extent) of downloaded images
manually_excluded_scenes = []       # manually excluded item id

def p(data):
    print(json.dumps(data, indent=2))

def get_and_set_Planet_key(user_account):
    keyfile = HOME+'/.planetkey'
    with open(keyfile) as f_obj:
        lines = f_obj.readlines()
        for line in lines:
            if user_account in line:
                key_str = line.split(':')[1]
                key_str = key_str.strip()       # remove '\n'
                os.environ["PL_API_KEY"] = key_str
                # set Planet API client
                global client
                client = api.ClientV1(api_key = key_str)

                return True
        raise ValueError('account: %s cannot find in %s'%(user_account,keyfile))

def search_scenes_on_server(idx, geom, start_date, end_date, cloud_cover_thr,item_types):
    # search and donwload using Planet Client API
    combined_filter = get_a_filter_cli_api(geom, start_date, end_date, cloud_cover_thr)

    # get the count number
    item_count = get_items_count(combined_filter, item_types)
    if item_count == 100000:
        basic.outputlogMessage('error, failed to get images of %dth polygon currently, skip it' % idx)
        return False
    basic.outputlogMessage('The total number of scenes is %d' % item_count)

    req = filters.build_search_request(combined_filter, item_types)
    # p(req)
    res = client.quick_search(req)

    return res, item_count


def get_items_count(combined_filter, item_types):
    '''
    based on the filter, and item types, the count of item
    :param combined_filter: filter
    :param item_types: item types
    :return: the count of items
    '''

    try:
        req = filters.build_search_request(combined_filter, item_types, interval="year") #year  or day
        stats = client.stats(req).get()
    except APIException as e:
        # basic.outputlogMessage(str(e))
        output_planetAPI_error(str(e))
        return 100000  # return a large number

    # p(stats)
    total_count = 0
    for bucket in stats['buckets']:
        total_count += bucket['count']
    return total_count

# try max 1000 times, wait rand from 1 to 10 seconds
@retry(stop_max_attempt_number=1000, wait_random_min=1000, wait_random_max=10000)
def get_assets_from_server(item):
    '''
    get assets from the servers
    :param item:
    :return:
    '''
    try :
        assets = client.get_assets(item).get()
    # except APIException as e:
    #     raise ValueError("Manually output the error: "+str(e))
    except:
        raise APIException
    return assets

# try max 1000 times, wait rand from 1 to 10 seconds
@retry(stop_max_attempt_number=1000, wait_random_min=1000, wait_random_max=10000)
def activate_a_asset_on_server(asset):
    '''
    activate a asset on the server (make it ready for download)
    :param asset:
    :return:
    '''
    try:
        res = client.activate(asset)
    except APIException as e:
        e_str = str(e)
        output_planetAPI_error(str(e))
        if "Download quota has been exceeded" in e_str:
            sys.exit(1)   # only exit this sub-process, not
            # quit(1)         # may exit the entire program (not working)
    except:
        raise APIException

    # print(activation.response.status_code)
    if int(res.response.status_code) == 401:
        basic.outputlogMessage('The account does not have permissions to download this file')
        return False

    if int(res.response.status_code) == 429:
        raise Exception("rate limit error")
    return True

# try max 1000 times, wait rand from 1 to 30 seconds
@retry(stop_max_attempt_number=1000, wait_random_min=1000, wait_random_max=30000)
def download_a_asset_from_server(item,assets,asset_key,save_dir):
    '''
    download a asset from the server
    :param item: the item
    :param assets: assets from get_assets_from_server
    :param asset_key: the name of the asset
    :param save_dir: save dir
    :return: True if successful, Flase otherwise
    '''

    proc_id = multiprocessing.current_process().pid
    print('Process: %d, start downloading %s (id: %s)'%(proc_id,asset_key,item['id']))
    output_stream = sys.stdout
    def download_progress(start=None,wrote=None,total=None, finish=None): #result,skip=None
        # print(start,wrote,total,finish)
        # if total:
        #     # print('received: %.2f K'%(float(total)/1024.0))
        #     output_stream.write('received: %.2f K'%(float(total)/1024.0))
        #     output_stream.flush()
        # if total:
        #     if finish is None:
        #         print('received: %.2f K'%(float(total)/1024.0), end='\r')
        #     else:
        #         print('received: %.2f K' % (float(total) / 1024.0))
        pass
    callback = api.write_to_file(directory=save_dir + '/', callback=download_progress) # save_dir + '/'  #
    body = client.download(assets[asset_key], callback=callback)
    # body.await() for version 1.1.0
    try:
        body.wait() # for version > 1.4.2
    except APIException as e:
        output_planetAPI_error('An APIException occurs when try to download %s (id: %s)'%(asset_key,item['id']))
        output_planetAPI_error(str(e))
        raise Exception("rate limit error or other API errors")
        # return False  # return a large number
    except:
        raise APIException

    return True


def read_polygons_json(polygon_shp, no_json=False):
    '''
    read polyogns and convert to json format
    :param polygon_shp: polygon in projection of EPSG:4326
    :param no_json: True indicate not json format
    :return:
    '''
    return vector_gpd.read_polygons_json(polygon_shp, no_json)

def output_planetAPI_error(message):
    logfile = 'planet_APIException.txt'
    timestr = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime() )
    outstr = timestr +': '+ message
    print(outstr)
    f=open(logfile,'a')
    f.writelines(outstr+'\n')
    f.close()

    pass

def get_a_filter_cli_api(polygon_json,start_date, end_date, could_cover_thr):
    '''
    create a filter based on a geometry, date range, cloud cover
    :param polygon_json: a polygon in json format
    :param start_date: start date
    :param end_date:  end date
    :param could_cover_thr: images with cloud cover less than this value
    :return:  a combined filter (and filter)
    '''

    # gt: Greater Than
    # gte: Greater Than or Equal To
    # lt: Less Than
    # lte: Less Than or Equal To

    geo_filter = filters.geom_filter(polygon_json)
    date_filter = filters.date_range('acquired', gte=start_date, lte = end_date)
    cloud_filter = filters.range_filter('cloud_cover', lte=could_cover_thr)

    combined_filters = filters.and_filter(geo_filter, date_filter, cloud_filter)

    return combined_filters


def activate_and_download_asset(item,assets,asset_key,save_dir,process_num):
    '''
    active a asset of a item and download it
    :param item: the item
    :param assets: assets from get_assets_from_server
    :param asset_key: the name of the asset
    :param save_dir: save dir
    :return: True if successful, Flase otherwise
    '''

    proc_id = multiprocessing.current_process().pid
    asset = assets.get(asset_key)

    # activate
    out = activate_a_asset_on_server(asset)
    if out is False:
        return False

    # wait until the asset has been activated
    asset_activated = False
    while asset_activated == False:
        # Get asset and its activation status
        assets = get_assets_from_server(item) # need to get the status from the server
        asset = assets.get(asset_key)
        asset_status = asset["status"]

        # If asset is already active, we are done
        if asset_status == 'active':
            asset_activated = True
            print("Process: %d, Asset is active and ready to download"%proc_id)

        # Still activating. Wait and check again.
        else:
            print("Process: %d, ...Still waiting for asset activation..."%proc_id)
            # time.sleep(3)
            waitime = random.randint(process_num, process_num + 30)
            time.sleep(waitime)

    return download_a_asset_from_server(item,assets,asset_key,save_dir)


def read_down_load_geometry(folder):
    '''
    read geojson files in a folder. geojson file stores the geometry of a file, and save to global varialbes
    :param folder: the save folder
    :return:
    '''
    global  downloaded_scene_geometry
    json_list = io_function.get_file_list_by_ext('.geojson',folder, bsub_folder=False)
    for json_file in json_list:

        # ignore the scenes in the excluded list
        item_id = os.path.splitext(os.path.basename(json_file))[0]
        if item_id in manually_excluded_scenes:
            continue

        scene_folder = os.path.splitext(json_file)[0]
        asset_files = io_function.get_file_list_by_pattern(scene_folder,'*')
        if len(asset_files) < 3:
            basic.outputlogMessage('downloading of scene %s is not compelte, ignore it'%item_id)
            continue

        with open(json_file) as json_file:
            data = json.load(json_file)
            # p(data) # test
            downloaded_scene_geometry.append(data)

def read_excluded_scenes(folder):
    '''
    manually excluded some scenes with small portion of cloud cover,
    because some of the scenes have cloud cover, but not shown in the metedata
    :param folder:
    :return:
    '''
    txt_path = os.path.join(folder,'manually_excluded_scenes.txt')
    global manually_excluded_scenes
    if os.path.isfile(txt_path):
        with open(txt_path,'r') as f_obj:
            lines = f_obj.readlines()
            for line in lines:
                if '#' in line or len(line) < 6:
                    continue
                manually_excluded_scenes.append(line.strip())
    else:
        basic.outputlogMessage('Warning, %s file does not exist'%txt_path)


def check_geom_polygon_overlap(boundary_list, polygon):
    '''
    check if a polygon is covered by any polygons in a geom_list
    :param boundary_list:  a list containing polygon
    :param polygon: a polygon
    :return: True if the polygon was cover a polyon by other, False otherwise
    '''

    # convert from json format to shapely
    polygon_shapely = shape(polygon)

    # using shapely to check the overlay
    for geom in boundary_list:
        geom_shapely = shape(geom)
        if geom_shapely.contains(polygon_shapely):
            return True

    return False

def get_downloadable_assets(scene_item):
    permissions = scene_item['_permissions']
    # e.g., assets.analytic:download  remove: assets and download
    valid_assets = [ item.split(':')[0].split('.')[1] for item in permissions]
    return valid_assets

def select_items_to_download(idx, cloud_cover_thr, polygon, all_items):
    """
    choose which item to download
    :param idx: the polygon
    :param cloud_cover_thr, cloud cover threshold
    :param polygon: the polygon
    :param all_items: item list
    :return: item list if find items to download, false otherwise
    """
    if len(all_items) < 1:
        basic.outputlogMessage('No inquiry results for %dth polygon' % idx)
        return False

    # Update on 5 November 2020
    # for some of the scenes, cloud cover is not the real cloud cover,
    # maybe due to Usable Data Masks https://developers.planet.com/docs/data/udm-2/
    # in this case, we should use 'cloud_percent' (int 0-100), otherwise, use 'cloud_cover' (double, 0-1)

    cloud_key = 'cloud_cover'  # double 0-1
    cloud_percent_count = 0
    cloud_cover_count = 0
    all_count = len(all_items)
    for item in all_items:
        if 'cloud_percent' in item['properties']:
            cloud_percent_count += 1
        if 'cloud_cover' in item['properties']:
            cloud_cover_count += 1

    if cloud_percent_count == all_count:
        cloud_key = 'cloud_percent'     # int 0-100
        basic.outputlogMessage('Warning, cloud_percent exists and would be used (cloud_cover will be ignored), maybe these images are acquired after August 2018')
    elif cloud_percent_count > all_count/2:
        cloud_key = 'cloud_percent'  # int 0-100
        basic.outputlogMessage('Warning, more than half scenes have cloud_percent (only %d out of %d), %d ones have cloud_cover, cloud_percent will be used'
                               %(cloud_percent_count,all_count,cloud_cover_count))

        # remove items without cloud_percent
        all_items = [ item for item in all_items if 'cloud_percent' in item['properties']]
        basic.outputlogMessage('Warning, removed %d scenes without cloud_percent, remain %d ones'%(all_count-len(all_items), len(all_items)))
        all_count = len(all_items)

    else:
        basic.outputlogMessage('Warning, cloud_percent exists, but only %d out of %d (less than half), %d ones have cloud_cover, cloud_cover will be used'
                               % (cloud_percent_count, len(all_items), cloud_cover_count))


    # sort the item based on cloud cover
    all_items.sort(key=lambda x: float(x['properties'][cloud_key]))
    # [print(item['id'],item['properties'][cloud_key]) for item in all_items]

    # for item in all_items:
    #     print(item)
    pre_sel_cloud_list = [str(item['properties'][cloud_key]) for item in all_items]
    basic.outputlogMessage('Before selection, could covers after sort: %s'%'_'.join(pre_sel_cloud_list))

    # items with surface
    all_items_sr = []
    all_items_NOsr = []
    items_other = []
    for item in all_items:
        valid_assets = get_downloadable_assets(item)
        if 'analytic_sr' in valid_assets:
            all_items_sr.append(item)
            continue
        if 'analytic' in valid_assets:
            all_items_NOsr.append(item)
        else:
            items_other.append(item)

    # put the one with 'analytic_sr' before others
    all_items = []
    all_items.extend(all_items_sr)
    all_items.extend(all_items_NOsr)
    all_items.extend(items_other)
    basic.outputlogMessage('Among the scenes, %d, %d, and %d of them have analytic_sr, only have analytic, '
                           'and do not have analytic or analytic_sr asset'%(len(all_items_sr), len(all_items_NOsr),len(items_other)))

    # convert from json format to shapely
    polygon_shapely = shape(polygon)

    # consider the coverage
    total_intersect_area = 0
    merged_item_extent = None
    selected_items = []
    for item in all_items:
        # print(item['id'])
        if item['id'] in manually_excluded_scenes:
            continue

        geom = item['geometry']
        geom_shapely = shape(geom)

        # extent the coverage
        if merged_item_extent is None:
            merged_item_extent = geom_shapely
        else:
            # merged_item_extent.union(geom_shapely)
            merged_item_extent = merged_item_extent.union(geom_shapely)
            # merged_item_extent = merged_item_extent.cascaded_union(geom_shapely)

        # calculate the intersection
        intersect = polygon_shapely.intersection(merged_item_extent)
        # print('intersect.area',intersect.area, 'total_intersect_area', total_intersect_area, 'polygon_shapely.area',polygon_shapely.area)
        if intersect.area > total_intersect_area:
            total_intersect_area = intersect.area
            selected_items.append(item)

        if total_intersect_area >= polygon_shapely.area:
            break

    # remove some scenes with cloud cover greater than cloud_cover_thr.
    # We also used cloud_cover_thr (0-1) when inquiring images, this may apply to 'cloud_cover' key (double 0-1), but this 'cloud_cover' may wrong
    # we sort the images based on cloud cover, but still may have some scenes has large cloud cover based on 'cloud_percent'(int 0-100)
    # here, we remove scenes 'cloud_percent' > cloud_cover_thr*100
    if cloud_key == 'cloud_percent':
        cloud_cover_thr_int = int(cloud_cover_thr * 100)
        count_before = len(selected_items)
        selected_items = [item  for item in selected_items if item['properties'][cloud_key] < cloud_cover_thr_int ]
        count_after = len(selected_items)
        basic.outputlogMessage('After sorting (cloud), selecting based on geometry, '
                               'still remove %d scenes based on cloud_percent smaller than %d'%((count_before-count_after),cloud_cover_thr_int))

    if len(selected_items) < 1:
        basic.outputlogMessage('No inquiry results for %dth polygon after selecting results' % idx)
        return False

    sel_cloud_list = [str(item['properties'][cloud_key]) for item in selected_items]
    basic.outputlogMessage('After selection, could covers of images are: %s'%'_'.join(sel_cloud_list))

    return selected_items


def check_asset_exist(download_item, asset, save_dir):
    '''
    check weather a asset already exist
    :param download_item:
    :param asset:
    :param save_dir:
    :return:
    '''

    # asset_types = ['analytic_sr', 'analytic_xml', 'udm', 'visual', 'visual_xml]
    id = download_item['id']
    if asset=='analytic_sr':
        output_name = id + '_3B_AnalyticMS_SR.tif'
    elif asset=='analytic':
        output_name = id + '_3B_AnalyticMS.tif'
    elif asset=='analytic_xml':
        output_name = id + '_3B_AnalyticMS_metadata.xml'
    elif asset=='udm':
        output_name = id + '_3B_AnalyticMS_DN_udm.tif'
    elif asset=='visual':
        output_name = id + '_3B_Visual.tif'
    elif asset=='visual_xml':
        output_name = id + '_3B_Visual_metadata.xml'
    else:
        raise ValueError('unsupported asset type')
        # basic.outputlogMessage('unsupported asset type')
        # return False

    if os.path.isfile(os.path.join(save_dir, output_name)):
        basic.outputlogMessage('file %s exist (item id: %s), skip downloading'%(output_name,id))
        return True
    else:
        return False



def download_planet_images(polygons_json, start_date, end_date, cloud_cover_thr, item_types, save_folder,process_num):
    '''
    download images from for all polygons, to save quota, each polygon only downlaod one image
    :param polygons_json: a list of polygons in json format
    :param start_date:
    :param end_date:
    :param cloud_cover_thr:
    :param save_folder:
    :return: True if successful, false otherwise
    '''

    for idx, geom in enumerate(polygons_json):

        # for test
        # if idx > 20: break
        # if idx != 1: continue
        # if idx != 344: continue

        ####################################
        #check if any image already cover this polygon, if yes, skip downloading
        if check_geom_polygon_overlap(downloaded_scene_geometry, geom) is True:
            basic.outputlogMessage('%dth polygon already in the extent of downloaded images, skip it'%idx)
            continue

        res, item_count = search_scenes_on_server(idx, geom, start_date, end_date, cloud_cover_thr,item_types)
        if res is False:
            continue
        if res.response.status_code == 200:
            all_items = []
            for item in res.items_iter(item_count):
                # print(item['id'], item['properties']['item_type'])
                all_items.append(item)


            # I want to download SR, level 3B, product
            select_items = select_items_to_download(idx,cloud_cover_thr, geom, all_items)
            if select_items is False:
                continue
            basic.outputlogMessage('After selection, the number of images need to download is %d' % len(select_items))
            if select_items is False:
                continue

            sub_tasks = []
            for download_item in select_items:
                download_item_id = download_item['id']
                # p(item['geometry'])
                save_dir = os.path.join(save_folder, download_item_id)
                save_geojson_path = os.path.join(save_folder, download_item_id + '.geojson')
                if os.path.isfile(save_geojson_path) and os.path.isdir(save_dir):
                    basic.outputlogMessage('scene %s has been downloaded: %s'%(download_item_id,save_dir))
                    continue

                os.system('mkdir -p ' + save_dir)
                assets = get_assets_from_server(download_item)
                basic.outputlogMessage('download a scene (id: %s) that cover the %dth polygon' % (download_item_id, idx))

                # check 'analytic_sr' is available, if not, d
                valid_assets = get_downloadable_assets(download_item)
                # print(valid_assets)
                download_asset_types = asset_types.copy()
                # if 'analytic_sr' not in valid_assets:
                #     basic.outputlogMessage('warning, analytic_sr is not available in the scene (id: %s), download analytic instead'%download_item_id)
                #     download_asset_types.remove('analytic_sr')
                #     download_asset_types.extend(supp_asset_types) # 'analytic'

                #####################################
                for asset in sorted(assets.keys()):
                    if asset not in download_asset_types:
                        continue
                    if check_asset_exist(download_item, asset, save_dir):
                        continue

                    #
                    # if activate_and_download_asset(download_item, assets, asset, save_dir,process_num):
                    #     basic.outputlogMessage('downloaded asset type: %s of scene (%s)' % (asset, download_item_id))
                    ############################################################
                    ##  parallel activate and download sub_tasks
                    while True:
                        if basic.alive_process_count(sub_tasks) < process_num:
                            sub_process = Process(target=activate_and_download_asset,args=(download_item, assets, asset, save_dir,process_num))
                            sub_process.start()
                            sub_tasks.append(sub_process)
                            # time.sleep(200)
                            # print('sleep 200')
                            break
                        else:
                            time.sleep(5) # wait, then try again.


                # save the geometry of this item to disk
                with open(save_geojson_path, 'w') as outfile:
                    json.dump(download_item['geometry'], outfile,indent=2)
                    # update the geometry of already downloaded geometry
                    downloaded_scene_geometry.append(download_item['geometry'])

            # wait until all task finished
            while basic.b_all_process_finish(sub_tasks) is False:
                print(datetime.now(),': wait all submitted tasks to finish ')
                time.sleep(10)

        else:
            print('code {}, text, {}'.format(res.response.status_code, res.response.text))

    return True

def main(options, args):

    polygons_shp = args[0]
    save_folder = args[1]  # folder for saving downloaded images

    # check training polygons
    assert io_function.is_file_exist(polygons_shp)
    os.system('mkdir -p ' + save_folder)

    item_types = options.item_types.split(',') # ["PSScene4Band"]  # , # PSScene4Band , PSOrthoTile

    start_date = datetime.strptime(options.start_date, '%Y-%m-%d') #datetime(year=2018, month=5, day=20)
    end_date = datetime.strptime(options.end_date, '%Y-%m-%d')  #end_date
    cloud_cover_thr = options.cloud_cover           # 0.3

    planet_account = options.planet_account
    process_num = options.process_num

    # set Planet API key
    get_and_set_Planet_key(planet_account)

    shp_prj = map_projection.get_raster_or_vector_srs_info_proj4(polygons_shp).strip()
    if shp_prj != '+proj=longlat +datum=WGS84 +no_defs':
        # reproject to 4326 projection
        basic.outputlogMessage('reproject %s to latlon'%polygons_shp)
        latlon_shp = io_function.get_name_by_adding_tail(polygons_shp,'latlon')
        if os.path.isfile(latlon_shp) is False:
            vector_gpd.reproject_shapefile(polygons_shp,'EPSG:4326',latlon_shp)
        polygons_shp = latlon_shp
        basic.outputlogMessage('save new shapefile to %s for downloading images' % polygons_shp)

    # read polygons
    polygons_json = read_polygons_json(polygons_shp)

    read_excluded_scenes(save_folder)   # read the excluded_scenes before read download images

    #read geometry of images already in "save_folder"
    read_down_load_geometry(save_folder)


    # download images
    download_planet_images(polygons_json, start_date, end_date, cloud_cover_thr, item_types, save_folder,process_num)

    #check each downloaded ones are completed, otherwise, remove the incompleted ones
    geojson_list = io_function.get_file_list_by_ext('.geojson',save_folder,bsub_folder=False)
    # print(geojson_list)
    incom_dir = os.path.join(save_folder, 'incomplete_scenes')

    for geojson_file in geojson_list:
        scene_id = os.path.splitext(os.path.basename(geojson_file))[0]
        scene_dir = os.path.join(save_folder,scene_id)
        files = io_function.get_file_list_by_pattern(scene_dir,scene_id+'*')
        # print(files)
        if len(files) != len(asset_types):
            if os.path.isdir(incom_dir):
                io_function.mkdir(incom_dir)

            basic.outputlogMessage('warning, downloading of %s is not completed, move to incomplete_scenes '%scene_id)
            io_function.movefiletodir(scene_dir,incom_dir,overwrite=True)
            io_function.movefiletodir(geojson_file,incom_dir,overwrite=True)


    test = 1



    pass

if __name__ == "__main__":

    usage = "usage: %prog [options] polygon_shp save_dir"
    parser = OptionParser(usage=usage, version="1.0 2019-10-01")
    parser.description = 'Introduction: search and download Planet images '
    parser.add_option("-s", "--start_date",default='2018-04-30',
                      action="store", dest="start_date",
                      help="start date for inquiry, with format year-month-day, e.g., 2018-05-23")
    parser.add_option("-e", "--end_date",default='2018-06-30',
                      action="store", dest="end_date",
                      help="the end date for inquiry, with format year-month-day, e.g., 2018-05-23")
    parser.add_option("-c", "--cloud_cover",
                      action="store", dest="cloud_cover", type=float,
                      help="the could cover threshold, only accept images with cloud cover less than the threshold")
    parser.add_option("-i", "--item_types",
                      action="store", dest="item_types",default='PSScene4Band',
                      help="the item types, e.g., PSScene4Band,PSOrthoTile")
    parser.add_option("-a", "--planet_account",
                      action="store", dest="planet_account",default='huanglingcao@link.cuhk.edu.hk',
                      help="planet email account, e.g., huanglingcao@link.cuhk.edu.hk")
    parser.add_option("-p", "--process_num",
                      action="store", dest="process_num",type=int,default=10,
                      help="number of processes to download images")



    (options, args) = parser.parse_args()
    if len(sys.argv) < 2 or len(args) < 1:
        parser.print_help()
        sys.exit(2)

    basic.setlogfile('download_planet_images_%s.log'%str(datetime.date(datetime.now())))

    main(options, args)
