"""
-----------------------------------------------------------------------
                              Esmeralda
-----------------------------------------------------------------------
From Spanish esmeralda (“emerald”), as first used in the novel Notre-Dame de Paris (1831) by Victor Hugo.
This city corrects hits energy and extracts topology information.
The input is penthesilea output containing hits, kdst global information and mc info.
The city outputs :
    - RECO corrected hits table, according to the given map and hits threshold and merging parameters
    - MC info (if run number <=0)
    - PAOLINA Summary of topological anlaysis containing:
        - corrected (paolina) hits, hits table after performing paolina drop_voxels
        - summary of per track information
        - summary of per event information
"""

import os
import tables as tb
import numpy  as np
import pandas as pd
import warnings

from functools   import partial
from typing      import Tuple
from typing      import Callable
from typing      import Optional

from .. reco                import tbl_functions        as tbl
from .. reco                import paolina_functions    as plf
from .. reco                import hits_functions       as hif
from .. reco                import corrections_new      as cof
from .. evm                 import event_model          as evm
from .. dataflow            import dataflow             as fl
from .. dataflow.dataflow   import push
from .. dataflow.dataflow   import pipe

from .  components import city
from .  components import print_every
from .  components import hits_and_kdst_from_files

from .. types.      ic_types import NN
from .. types.      ic_types import xy
from .. io.         hits_io  import          hits_writer
from .. io.       mcinfo_io  import       mc_info_writer
from .. io.run_and_event_io  import run_and_event_writer
from .. io. event_filter_io  import  event_filter_writer
from .. io.          dst_io  import _store_pandas_as_tables

def hits_threshold_and_corrector(map_fname        : str  ,
                                 threshold_charge : float,
                                 same_peak        : bool ,
                                 apply_temp       : bool
                                 ) -> Callable:
    """
    For a given correction map and hit threshold/ merging parameters returns a function that applies thresholding, merging and
    energy and Z corrections to a given HitCollection object.

    Parameters
    ----------
    map_fname        : string (filepath)
        filename of the map
    threshold_charge : float
        minimum pes of a hit
    same_peak        : bool
        if True energy of NN hits is assigned only to the hits from the same peak
    apply_temp       : bool
        whether to apply temporal corrections
        must be set to False if no temporal correction dataframe exists in map file
    norm_strat       :  norm_strategy
    class norm_strategy(AutoNameEnumBase):
        mean   = auto()
        max    = auto()
        kr     = auto()
        custom = auto()
    strategy to normalize the energy


    Returns
    ----------
    A function that takes HitCollection as input and returns HitCollection that containes
    only non NN hits of charge above threshold_charge with modified Ec and Z fields.
    """
    map_fname = os.path.expandvars(map_fname)
    maps      = cof.read_maps(map_fname)
    get_coef  = cof.apply_all_correction(maps, apply_temp = apply_temp, norm_strat = cof.norm_strategy.kr)
    if maps.t_evol is not None:
        time_to_Z = cof.get_df_to_z_converter(maps)
    else:
        time_to_Z = lambda x: x
    def threshold_and_correct_hits(hitc : evm.HitCollection) -> evm.HitCollection:
        t = hitc.time
        thr_hits = hif.threshold_hits(hitc.hits, threshold_charge     )
        mrg_hits = hif.merge_NN_hits ( thr_hits, same_peak = same_peak)
        X  = np.fromiter((h.X for h in mrg_hits), float)
        Y  = np.fromiter((h.Y for h in mrg_hits), float)
        Z  = np.fromiter((h.Z for h in mrg_hits), float)
        E  = np.fromiter((h.E for h in mrg_hits), float)
        Ec = E * get_coef(X,Y,Z,t)
        Zc = time_to_Z(Z)
        cor_hits = []
        for idx, hit in enumerate(mrg_hits):
            hit = evm.Hit(hit.npeak, evm.Cluster(hit.Q, xy(hit.X, hit.Y), hit.var, hit.nsipm),
                          Zc[idx], hit.E, xy(hit.Xpeak, hit.Ypeak), s2_energy_c = Ec[idx])
            cor_hits.append(hit)
        new_hitc      = evm.HitCollection(hitc.event, t)
        new_hitc.hits = cor_hits
        return new_hitc
    return threshold_and_correct_hits

def copy_Ec_to_Ep_hit_attribute_(hitc : evm.HitCollection) -> evm.HitCollection:
    """
    The functions copies values of Ec attributes into Ep attributes. Takes as input HitCollection and returns a copy.

    """
    mod_hits = []
    for hit in hitc.hits:
        hit = evm.Hit(hit.npeak, evm.Cluster(hit.Q, xy(hit.X, hit.Y), hit.var, hit.nsipm),
                      hit.Z, hit.E, xy(hit.Xpeak, hit.Ypeak), s2_energy_c=hit.Ec, Ep =hit.Ec)
        mod_hits.append(hit)
    mod_hitc = evm.HitCollection(hitc.event, hitc.time, hits=mod_hits)
    return mod_hitc


def track_blob_info_creator_extractor(vox_size         : [float, float, float],
                                      strict_vox_size  : bool                 ,
                                      energy_threshold : float                ,
                                      min_voxels       : int                  ,
                                      blob_radius      : float
                                     ) -> Callable:
    """
    For a given paolina parameters returns a function that extract tracks / blob information from a HitCollection.

    Parameters
    ----------
    vox_size         : [float, float, float]
        (maximum) size of voxels for track reconstruction
    strict_vox_size  : bool
        if False allows per event adaptive voxel size,
        smaller of equal thatn vox_size
    energy_threshold : float
        if energy of end-point voxel is smaller
        the voxel will be dropped and energy redistributed to the neighbours
    min_voxels       : int
        after min_voxel number of voxels is reached no dropping will happen.
    blob_radius      : float
        radius of blob

    Returns
    ----------
    A function that from a given HitCollection returns a pandas DataFrame with per track information.
    """
    def create_extract_track_blob_info(hitc):
        #track_hits is a new Hitcollection object that contains hits belonging to tracks, and hits that couldnt be corrected
        track_hitc = evm.HitCollection(hitc.event, hitc.time)
        out_of_map = any(np.isnan([h.Ep for h in hitc.hits]))
        if out_of_map:
            #add nan hits to track_hits, the track_id will be -1
            track_hitc.hits.extend  ([h for h in hitc.hits if np.isnan   (h.Ep)])
            hits_without_nan       = [h for h in hitc.hits if np.isfinite(h.Ep)]
            #create new Hitcollection object but keep the name hitc
            hitc      = evm.HitCollection(hitc.event, hitc.time)
            hitc.hits = hits_without_nan
        df = pd.DataFrame(columns=['event', 'trackID', 'energy', 'length', 'numb_of_voxels',
                                   'numb_of_hits', 'numb_of_tracks', 'x_min', 'y_min', 'z_min', 'r_min',
                                   'x_max', 'y_max', 'z_max', 'r_max', 'x_ave', 'y_ave', 'z_ave', 'r_ave',
                                   'extreme1_x', 'extreme1_y', 'extreme1_z',
                                   'extreme2_x', 'extreme2_y', 'extreme2_z',
                                   'blob1_x', 'blob1_y', 'blob1_z',
                                   'blob2_x', 'blob2_y', 'blob2_z',
                                   'eblob1', 'eblob2', 'ovlp_blob_energy',
                                   'vox_size_x', 'vox_size_y', 'vox_size_z'])

        if len(hitc.hits)>0:
            voxels           = plf.voxelize_hits(hitc.hits, vox_size, strict_vox_size, evm.HitEnergy.Ep)
            (    mod_voxels,
             dropped_voxels) = plf.drop_end_point_voxels(voxels, energy_threshold, min_voxels)
            tracks           = plf.make_track_graphs(mod_voxels)

            for v in dropped_voxels:
                track_hitc.hits.extend(v.hits)

            vox_size_x = voxels[0].size[0]
            vox_size_y = voxels[0].size[1]
            vox_size_z = voxels[0].size[2]
            del(voxels)
            #sort tracks in energy
            tracks     = sorted(tracks, key=plf.get_track_energy, reverse=True)

            track_hits = []
            for c, t in enumerate(tracks, 0):
                tID = c
                energy = plf.get_track_energy(t)
                length = plf.length(t)
                numb_of_hits   = len([h for vox in t.nodes() for h in vox.hits])
                numb_of_voxels = len(t.nodes())
                numb_of_tracks = len(tracks   )
                pos   = [h.pos for v in t.nodes() for h in v.hits]
                x, y, z = map(np.array, zip(*pos))
                r = np.sqrt(x**2 + y**2)

                e     = [h.Ep for v in t.nodes() for h in v.hits]
                ave_pos = np.average(pos, weights=e, axis=0)
                ave_r   = np.average(r  , weights=e, axis=0)
                extr1, extr2 = plf.find_extrema(t)
                extr1_pos = extr1.XYZ
                extr2_pos = extr2.XYZ

                blob_pos1, blob_pos2 = plf.blob_centres(t, blob_radius)

                e_blob1, e_blob2, hits_blob1, hits_blob2 = plf.blob_energies_and_hits(t, blob_radius)
                overlap = float(sum(h.Ep for h in set(hits_blob1).intersection(set(hits_blob2))))
                list_of_vars = [hitc.event, tID, energy, length, numb_of_voxels,
                                numb_of_hits, numb_of_tracks,
                                min(x), min(y), min(z), min(r), max(x), max(y), max(z), max(r),
                                *ave_pos, ave_r, *extr1_pos,
                                *extr2_pos, *blob_pos1, *blob_pos2,
                                e_blob1, e_blob2, overlap,
                                vox_size_x, vox_size_y, vox_size_z]

                df.loc[c] = list_of_vars
                try:
                    types_dict
                except NameError:
                    types_dict = dict(zip(df.columns, [type(x) for x in list_of_vars]))

                for vox in t.nodes():
                    for hit in vox.hits:
                        hit.track_id = tID
                        track_hits.append(hit)

            #change dtype of columns to match type of variables
            df = df.apply(lambda x : x.astype(types_dict[x.name]))
            track_hitc.hits.extend(track_hits)
        return df, track_hitc, out_of_map

    return create_extract_track_blob_info


def make_event_summary(event_number  : int              ,
                       topology_info : pd.DataFrame     ,
                       paolina_hits  : evm.HitCollection,
                       out_of_map    : bool
                       ) -> pd.DataFrame:
    """
    For a given event number, timestamp, topology info dataframe, paolina hits and kdst information returns a
    dataframe with the whole event summary.

    Parameters
    ----------
    event_number  : int
    timestamp     : long int
    topology_info : DataFrame
        Dataframe containing track information,
        output of track_blob_info_creator_extractor
    paolina_hits  : HitCollection
        Hits table passed through paolina functions,
        output of track_blob_info_creator_extractor
    kdst          : DataFrame
        Kdst information read from penthesilea output


    Returns
    ----------
    DataFrame containing relevant per event information.
    """
    es = pd.DataFrame(columns=['event', 'evt_energy' ,  'evt_charge', 'evt_ntrks', 'evt_nhits',
                               'evt_x_avg', 'evt_y_avg', 'evt_z_avg', 'evt_r_avg',
                               'evt_x_min', 'evt_y_min', 'evt_z_min', 'evt_r_min',
                               'evt_x_max', 'evt_y_max', 'evt_z_max', 'evt_r_max',
                               'evt_out_of_map'])

    ntrks = len(topology_info.index)
    nhits = len(paolina_hits.hits)

    S2ec = max(-1, sum(h.Ec for h in paolina_hits.hits))
    S2qc = max(-1, sum(h.Qc for h in paolina_hits.hits))

    pos   = [h.pos for h in paolina_hits.hits]
    x, y, z = map(np.array, zip(*pos))
    r = np.sqrt(x**2 + y**2)

    e     = [h.Ec  for h in paolina_hits.hits]
    ave_pos = np.average(pos, weights=e, axis=0)
    ave_r   = np.average(r  , weights=e, axis=0)


    list_of_vars  = [event_number, S2ec, S2qc, ntrks, nhits,
                     *ave_pos, ave_r,
                     min(x), min(y), min(z), min(r), max(x), max(y), max(z), max(r),
                     out_of_map]

    es.loc[0] = list_of_vars
    #change dtype of columns to match type of variables
    types_dict = dict(zip(es.columns, [type(x) for x in list_of_vars]))
    es = es.apply(lambda x : x.astype(types_dict[x.name]))

    return es


def track_writer(h5out, compression='ZLIB4', group_name='Tracking', table_name='Tracks', descriptive_string='Track information', str_col_length=32):
    """
    For a given open table returns a writer for topology info dataframe
    """
    def write_tracks(df):
        return _store_pandas_as_tables(h5out=h5out, df=df, compression=compression, group_name=group_name, table_name=table_name, descriptive_string=descriptive_string, str_col_length=str_col_length)
    return write_tracks


def summary_writer(h5out, compression='ZLIB4', group_name='Summary', table_name='Events', descriptive_string='Event summary information', str_col_length=32):
    """
    For a given open table returns a writer for summary info dataframe
    """
    def write_summary(df):
        return _store_pandas_as_tables(h5out=h5out, df=df, compression=compression, group_name=group_name, table_name=table_name, descriptive_string=descriptive_string, str_col_length=str_col_length)
    return write_summary

def kdst_from_df_writer(h5out, compression='ZLIB4', group_name='DST', table_name='Events', descriptive_string='KDST Events', str_col_length=32):
    """
    For a given open table returns a writer for KDST dataframe info
    """
    def write_kdst(df):
        return _store_pandas_as_tables(h5out=h5out, df=df, compression=compression, group_name=group_name, table_name=table_name, descriptive_string=descriptive_string, str_col_length=str_col_length)
    return write_kdst


@city
def esmeralda(files_in, file_out, compression, event_range, print_mod, run_number,
              cor_hits_params = dict(),
              paolina_params  = dict()):
    """
    The city corrects Penthesilea hits energy and extracts topology information.
    ----------
    Parameters
    ----------
    files_in  : str, filepath
         input file
    file_out  : str, filepath
         output file
    compression : str
         Default  'ZLIB4'
    event_range : int /'all_events'
         number of events from files_in to process
    print_mode : int
         how frequently to print events
    run_number : int
         has to be negative for MC runs

    cor_hits_params              : dict
        map_fname                : string (filepath)
            filename of the map
        threshold_charge_low     : float
            minimum pes for a RECO hit
        threshold_charge_high    : float
            minimum pes for a PAOLINA hit
        same_peak                : bool
            if True energy of NN hits is assigned only to the hits from the same peak
        apply_temp               : bool
            whether to apply temporal corrections
            must be set to False if no temporal correction dataframe exists in map file
        norm_strat               : 'max', 'mean' or 'kr'
           strategy to normalize the energy

    paolina_params               :dict
        vox_size                 : [float, float, float]
            (maximum) size of voxels for track reconstruction
        strict_vox_size          : bool
            if False allows per event adaptive voxel size,
            smaller of equal thatn vox_size
        energy_threshold        : float
            if energy of end-point voxel is smaller
            the voxel will be dropped and energy redistributed to the neighbours
        min_voxels              : int
            after min_voxel number of voxels is reached no dropping will happen.
        blob_radius             : float
            radius of blob
    ----------
    Input
    ----------
    Penthesilea output
    ----------
    Output
    ----------
    RECO    : corrected hits table, according to the given map and hits threshold and merging parameters
    MC info : (if run number <=0)
    PAOLINA : Summary of topological anlaysis containing:
        Events  : corrected (paolina) hits, hits table after performing paolina drop_voxels
        Tracks  : summary  of per track topological information
        Summary : summary of per event information

"""


    cor_hits_params_   = {value : cor_hits_params.get(value) for value in ['map_fname', 'same_peak', 'apply_temp']}

    threshold_and_correct_hits_low  = fl.map(hits_threshold_and_corrector(threshold_charge=cor_hits_params['threshold_charge_reco'   ], **cor_hits_params_),
                                             args = 'hits',
                                             out  = 'cor_low_th_hits')

    threshold_and_correct_hits_high = fl.map(hits_threshold_and_corrector(threshold_charge=cor_hits_params['threshold_charge_paolina'], **cor_hits_params_),
                                             args = 'hits',
                                             out  = 'cor_high_th_hits')

    filter_events_low_th            = fl.map(lambda x : len(x.hits) > 0,
                                             args = 'cor_low_th_hits',
                                             out  = 'low_th_hits_passed')

    filter_events_high_th           = fl.map(lambda x : len(x.hits) > 0,
                                             args = 'cor_high_th_hits',
                                             out  = 'high_th_hits_passed')

    hits_passed_low_th              = fl.count_filter(bool, args="low_th_hits_passed")
    hits_passed_high_th             = fl.count_filter(bool, args="high_th_hits_passed")

    copy_Ec_to_Ep_hit_attribute     = fl.map(copy_Ec_to_Ep_hit_attribute_,
                                             args = 'cor_high_th_hits',
                                             out  = 'cor_Ep_high_th_hits')

    create_extract_track_blob_info  = fl.map(track_blob_info_creator_extractor(**paolina_params),
                                             args = 'cor_Ep_high_th_hits',
                                             out  = ('topology_info', 'paolina_hits', 'out_of_map'))
    filter_events_topology          = fl.map(lambda x : len(x) > 0,
                                             args = 'topology_info',
                                             out  = 'topology_passed')
    events_passed_topology          = fl.count_filter(bool, args="topology_passed")

    make_final_summary              = fl.map(make_event_summary,
                                             args = ('event_number', 'topology_info', 'paolina_hits', 'out_of_map'),
                                             out  = 'event_info')

    event_count_in  = fl.spy_count()
    event_count_out = fl.spy_count()

    with tb.open_file(file_out, "w", filters=tbl.filters(compression)) as h5out:

        # Define writers...
        write_event_info = fl.sink(run_and_event_writer(h5out), args=("run_number", "event_number", "timestamp"))
        write_mc_        = mc_info_writer(h5out) if run_number <= 0 else (lambda *_: None)

        write_hits_low_th     = fl.sink(    hits_writer     (h5out, group_name='CHITS', table_name='lowTh'),
                                            args="cor_low_th_hits")
        write_hits_paolina    = fl.sink(    hits_writer     (h5out, group_name='CHITS', table_name='highTh' ),
                                            args="paolina_hits"   )
        write_mc              = fl.sink(    write_mc_                                    , args=("mc", "event_number"))
        write_tracks          = fl.sink(   track_writer     (h5out=h5out)                , args="topology_info"      )
        write_summary         = fl.sink( summary_writer     (h5out=h5out)                , args="event_info"         )
        write_high_th_filter  = fl.sink( event_filter_writer(h5out, "high_th_select" )   , args=("event_number", "high_th_hits_passed"))
        write_low_th_filter   = fl.sink( event_filter_writer(h5out, "low_th_select"  )   , args=("event_number", "low_th_hits_passed" ))
        write_topology_filter = fl.sink( event_filter_writer(h5out, "topology_select")   , args=("event_number", "topology_passed"    ))
        write_kdst_table      = fl.sink( kdst_from_df_writer(h5out)                      , args="kdst"               )

        return  push(source = hits_and_kdst_from_files(files_in),
                     pipe   = pipe(
                         fl.slice(*event_range, close_all=True)       ,
                         print_every(print_mod)                       ,
                         event_count_in        .spy                   ,
                         fl.branch(fl.fork(write_kdst_table           ,
                                           write_event_info           ,
                                           write_mc                 )),
                         fl.branch(threshold_and_correct_hits_low     ,
                                   filter_events_low_th               ,
                                   fl.branch(write_low_th_filter)     ,
                                   hits_passed_low_th.filter          ,
                                   write_hits_low_th                 ),
                         threshold_and_correct_hits_high              ,
                         filter_events_high_th                        ,
                         fl.branch(write_high_th_filter)              ,
                         hits_passed_high_th   .filter                ,
                         copy_Ec_to_Ep_hit_attribute                  ,
                         create_extract_track_blob_info               ,
                         filter_events_topology                       ,
                         fl.branch(write_hits_paolina)                ,
                         events_passed_topology.filter                ,
                         event_count_out       .spy                   ,
                         fl.branch(write_topology_filter)             ,
                         fl.fork( write_tracks                        ,
                                 (make_final_summary, write_summary))),

                     result = dict(events_in =event_count_in .future ,
                                   events_out=event_count_out.future ))
