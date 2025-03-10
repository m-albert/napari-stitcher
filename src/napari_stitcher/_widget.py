"""
This module is an example of a barebones QWidget plugin for napari

It implements the Widget specification.
see: https://napari.org/stable/plugins/guides.html?#widgets

Replace code below according to your needs.
"""
from typing import TYPE_CHECKING
import os, tempfile, sys

import numpy as np
import dask

from napari.utils import notifications

from magicgui import widgets
from qtpy.QtWidgets import QVBoxLayout, QWidget

import spatial_image as si

from multiview_stitcher import (
    registration,
    fusion,
    spatial_image_utils,
    msi_utils,
    )

from napari_stitcher import _reader, viewer_utils, _utils

if TYPE_CHECKING:
    import napari


# define labels for visualization choices
CHOICE_METADATA = 'Original'
CHOICE_REGISTERED = 'Registered'


class StitcherQWidget(QWidget):
    # your QWidget.__init__ can optionally request the napari viewer instance
    # in one of two ways:
    # 1. use a parameter called `napari_viewer`, as done here
    # 2. use a type annotation of 'napari.viewer.Viewer' for any parameter
    def __init__(self, napari_viewer):
        super().__init__()
        self.viewer = napari_viewer

        self.setLayout(QVBoxLayout())

        self.button_load_layers_all = widgets.Button(text='All')
        self.button_load_layers_sel = widgets.Button(text='Selected')
        self.buttons_load_layers = widgets.HBox(
            widgets=\
                [self.button_load_layers_sel,
                    self.button_load_layers_all]
                    )
        self.layers_selection = widgets.Select(choices=[])
        self.load_layers_box = widgets.VBox(widgets=\
                                            [
            self.buttons_load_layers,
            self.layers_selection,
                                            ],
                                            label='Loaded\nlayers:')

        self.times_slider = widgets.RangeSlider(
            min=-1, max=0, label='Timepoints:', enabled=False,
            tooltip='Timepoints to process. Because the two sliders cannot coincide, positions are a bit criptic: E.g.\n(-1, 0) means timepoint 0 is processed\n(3, 5) means timepoints 4 and 5 are processed')
        
        self.reg_ch_picker = widgets.ComboBox(
            label='Reg channel: ',
            choices=[],
            tooltip='Choose a file to process using napari-stitcher.')

        self.button_stitch = widgets.Button(text='Register', enabled=False,
            tooltip='Use the overlaps between tiles to determine their relative positions.')
        
        # self.button_stabilize = widgets.Button(text='Stabilize', enabled=False,
        #     tooltip='Use time lapse information to stabilize each tile over time,'+\
        #             'eliminating abrupt shifts between frames. No tile overlap needed.')

        self.buttons_register_tracks = widgets.HBox(
            widgets=[
                    self.button_stitch,
                    # self.button_stabilize
                    ]
                    )

        self.visualization_type_rbuttons = widgets.RadioButtons(
            choices=[CHOICE_METADATA, CHOICE_REGISTERED],
            label="Show:",
            value=CHOICE_METADATA, enabled=False,
            orientation='horizontal')

        self.button_fuse = widgets.Button(text='Fuse', enabled=False,
            tooltip='Fuse the tiles using the parameters obtained'+\
                    'from stitching or stabilization.\nCombines all'+\
                    'tiles and timepoints into a single image, smoothly'+\
                    'blending the overlaps and filling in gaps.')

        self.loading_widgets = [
                            self.load_layers_box,
                            ]

        self.reg_widgets = [
                            self.times_slider,
                            self.reg_ch_picker,
                            self.buttons_register_tracks,
                            ]

        self.visualization_widgets = [
                            self.visualization_type_rbuttons,
        ]

        self.fusion_widgets = [
                            widgets.HBox(widgets=[self.button_fuse]),
                            ]


        self.container = widgets.VBox(widgets=\
                            self.loading_widgets+
                            self.reg_widgets+
                            self.visualization_widgets+
                            self.fusion_widgets
                            )

        self.container.native.setMinimumWidth = 50

        self.layout().addWidget(self.container.native)

        # initialize registration parameter dict
        self.input_layers= []
        self.msims = {}
        self.fused_layers = []
        self.params = dict()

        # create temporary directory for storing dask arrays
        self.tmpdir = tempfile.TemporaryDirectory()
        
        self.visualization_type_rbuttons.changed.connect(self.update_viewer_transformations)
        self.viewer.dims.events.connect(self.update_viewer_transformations)

        self.button_stitch.clicked.connect(self.run_registration)
        # self.button_stabilize.clicked.connect(self.run_stabilization)
        self.button_fuse.clicked.connect(self.run_fusion)

        self.button_load_layers_all.clicked.connect(self.load_layers_all)
        self.button_load_layers_sel.clicked.connect(self.load_layers_sel)


    def update_viewer_transformations(self):
        """
        set transformations
        - for current timepoint
        - for each (compatible) layer loaded in viewer
        """

        if not len(self.msims): return

        compatible_layers = [l for l in self.viewer.layers
                             if l.name in self.msims.keys()]
        
        if not len(compatible_layers): return
        
        sims = [msi_utils.get_sim_from_msim(self.msims[l.name])
                for l in compatible_layers]

        # determine spatial dimensions from layers
        all_spatial_dims = [spatial_image_utils.get_spatial_dims_from_sim(
            sims[il])
            for il, l in enumerate(compatible_layers)]
        
        highest_sdim = max([len(sdim) for sdim in all_spatial_dims])

        # get curr tp
        # handle possibility that there had been no T dimension
        # when collecting sims from layers

        if len(self.viewer.dims.current_step) > highest_sdim:
            curr_tp = self.viewer.dims.current_step[-highest_sdim-1]
        else:
            curr_tp = 0

        if self.visualization_type_rbuttons.value == CHOICE_METADATA:
            transform_key=_reader.METADATA_TRANSFORM_KEY
        else:
            transform_key = 'affine_registered'

        for il, l in enumerate(compatible_layers):

            try:
                params = spatial_image_utils.get_affine_from_sim(
                    sims[il], transform_key=transform_key
                    )
            except:
                # notifications.notification_manager.receive_info(
                #     'Update transform: %s not available in %s' %(transform_key, l.name))
                continue

            try:
                p = np.array(params.sel(t=sims[il].coords['t'][curr_tp])).squeeze()
            except:
                notifications.notification_manager.receive_info(
                    'Timepoint %s: no parameters available, register first.' % curr_tp)
                continue

                # # if curr_tp not available, use nearest available parameter
                # notifications.notification_manager.receive_info(
                #     'Timepoint %s: no parameters available, taking nearest available one.' % curr_tp)
                # p = np.array(params.sel(t=layer_sim.coords['t'][curr_tp], method='nearest')).squeeze()

            ndim_layer_data = l.ndim

            # if stitcher sim has more dimensions than layer data (i.e. time)
            vis_p = p[-(ndim_layer_data + 1):, -(ndim_layer_data + 1):]

            # if layer data has more dimensions than stitcher sim
            full_vis_p = np.eye(ndim_layer_data + 1)
            full_vis_p[-len(vis_p):, -len(vis_p):] = vis_p

            l.affine.affine_matrix = full_vis_p

            # refreshing layers fails sometimes
            # this solution is suboptimal though
            try:
                l.refresh()
            except:
                pass


    def run_registration(self):

        msims_dict = {_utils.get_str_unique_to_view_from_layer_name(lname): msim
                      for lname, msim in self.msims.items()
                      if self.reg_ch_picker.value in msi_utils.get_sim_from_msim(msim).coords['c']}
        
        sorted_lnames = sorted(list(msims_dict.keys()))

        msims = [msims_dict[lname] for lname in sorted_lnames]

        msims = [msi_utils.multiscale_sel_coords(msim,
                {'t': [msi_utils.get_sim_from_msim(msim).coords['t'][it]
                         for it in range(self.times_slider.value[0] + 1,
                                         self.times_slider.value[1] + 1)]})
                  for msim in msims]

        with _utils.TemporarilyDisabledWidgets([self.container]),\
            _utils.VisibleActivityDock(self.viewer),\
            _utils.TqdmCallback(tqdm_class=_utils.progress,
                                desc='Registering tiles', bar_format=" "):
            
            params = registration.register(
                msims,
                # registration_binning={'z': 2, 'y': 8, 'x': 8},
                registration_binning=None,
                transform_key='affine_metadata',
            )

        for lname, msim in self.msims.items():
            params_index = sorted_lnames.index(_utils.get_str_unique_to_view_from_layer_name(lname))
            msi_utils.set_affine_transform(
                msim, params[params_index],
                transform_key='affine_registered', base_transform_key='affine_metadata')
            
        for l in self.input_layers:
            params_index = sorted_lnames.index(_utils.get_str_unique_to_view_from_layer_name(l.name))
            try:
                viewer_utils.set_layer_xaffine(
                    l, params[params_index],
                    transform_key='affine_registered', base_transform_key='affine_metadata')
            except:
                pass

        # if not len(g_reg.edges):
        #     message = 'No overlap between views for stitching. Consider stabilizing the tiles instead.'
        #     notifications.notification_manager.receive_info(message)
        #     return
        
        self.visualization_type_rbuttons.enabled = True
        self.visualization_type_rbuttons.value = CHOICE_REGISTERED


    def run_fusion(self):

        """
        Split layers into channel groups and fuse each group separately.
        """

        channels = self.reg_ch_picker.choices

        for _, ch in enumerate(channels):

            msims = [msim for _, msim in self.msims.items()
                    if ch in msi_utils.get_sim_from_msim(msim).coords['c']]

            sims = [msi_utils.get_sim_from_msim(msim) for msim in msims]

            sims = [spatial_image_utils.sim_sel_coords(sim,
                    {'t': [sim.coords['t'][it]
                            for it in range(self.times_slider.value[0] + 1,
                                            self.times_slider.value[1] + 1)]})
                    for sim in sims]

            fused = fusion.fuse(
                sims,
                transform_key='affine_registered'
                if self.visualization_type_rbuttons.value == CHOICE_REGISTERED
                else 'affine_metadata',
            )

            fused = fused.expand_dims({'c': [sims[0].coords['c'].values]})

            mfused = msi_utils.get_msim_from_sim(fused, scale_factors=[])

            tmp_fused_path = os.path.join(self.tmpdir.name, 'fused_%s.zarr' %ch)

            with _utils.TemporarilyDisabledWidgets([self.container]),\
                _utils.VisibleActivityDock(self.viewer),\
                _utils.TqdmCallback(tqdm_class=_utils.progress,
                                    desc='Fusing tiles of channel %s' %ch, bar_format=" "):
                
                mfused.to_zarr(tmp_fused_path)

            mfused = msi_utils.multiscale_spatial_image_from_zarr(tmp_fused_path)

            fused_ch_layer_tuple = viewer_utils.create_image_layer_tuples_from_msim(
                mfused,
                colormap=None,
                name_prefix='fused',
            )[0]

            fused_layer = self.viewer.add_image(fused_ch_layer_tuple[0], **fused_ch_layer_tuple[1])
        
            self.fused_layers.append(fused_layer)


    def reset(self):
            
        self.msims = {}
        self.params = dict()
        self.reg_ch_picker.choices = ()
        self.visualization_type_rbuttons.value = CHOICE_METADATA
        self.times_slider.min, self.times_slider.max = (-1, 0)
        self.times_slider.value = (-1, 0)
        self.input_layers = []
        self.fused_layers = []


    def load_metadata(self):
        
        reference_sim = msi_utils.get_sim_from_msim(self.msims[self.input_layers[0].name])
        
        # assume dims are the same for all layers
        if 't' in reference_sim.dims:
            self.times_slider.enabled = True
            self.times_slider.min = -1
            self.times_slider.max = len(reference_sim.coords['t']) - 1
            self.times_slider.value = self.times_slider.min, self.times_slider.max

        if 'c' in reference_sim.coords.keys():
            self.reg_ch_picker.enabled = True
            self.reg_ch_picker.choices = np.unique([
                _utils.get_str_unique_to_ch_from_sim_coords(msi_utils.get_sim_from_msim(msim).coords)
                for l_name, msim in self.msims.items()])
            self.reg_ch_picker.value = self.reg_ch_picker.choices[0]

        from collections.abc import Iterable
        for w in self.reg_widgets + self.fusion_widgets:
            if isinstance(w, Iterable):
                for sw in w:
                    sw.enabled = True
            w.enabled = True


    def load_layers_all(self):

        if not len(self.viewer.layers):
            notifications.notification_manager.receive_info(
                'No images in the layer list.'
            )
            return

        self.load_layers(self.viewer.layers)


    def load_layers_sel(self):

        if not len(self.viewer.layers.selection):
            notifications.notification_manager.receive_info(
                'Select layers from the layer list (mutliple using shift / %s'\
                    %('control' if ('command' in sys.platform) else 'shift')
            )
            return

        self.load_layers([l for l in self.viewer.layers.selection])


    def load_layers(self, layers):

        self.reset()
        self.layers_selection.choices = sorted([l.name for l in layers])

        self.input_layers = [l for l in layers]

        # load in layers as sims
        for l in layers:

            msim = viewer_utils.image_layer_to_msim(l, self.viewer)
            
            if 'c' in msim['scale0/image'].dims:
                notifications.notification_manager.receive_info(
                    "Layer '%s' has more than one channel.Consider splitting the stack (right click on layer -> 'Split Stack')." %l.name
                )
                self.layers_selection.choices = []
                self.reset()
                return
            
            msim = msi_utils.ensure_time_dim(msim)
            self.msims[l.name] = msim

        sims = [msi_utils.get_sim_from_msim(msim) for l.name, msim in self.msims.items()]

        number_of_channels = len(np.unique([
            _utils.get_str_unique_to_ch_from_sim_coords(sim.coords)
                for sim in sims]))
        
        if len(layers) and number_of_channels > 1:
            self.link_channel_layers(layers)

        self.load_metadata()


    def link_channel_layers(self, layers):

        # link channel layers
        from napari.experimental import link_layers

        sims = {l.name: msi_utils.get_sim_from_msim(self.msims[l.name])
                for l in layers}

        channels = [_utils.get_str_unique_to_ch_from_sim_coords(sim.coords) for sim in sims.values()]
        for ch in channels:
            ch_layers = list(_utils.filter_layers(layers, sims, ch=ch))

            if len(ch_layers):
                link_layers(ch_layers, ('contrast_limits', 'visible'))


    def __del__(self):

        print('Deleting napari-stitcher widget')

        # clean up callbacks
        self.viewer.dims.events.disconnect(self.update_viewer_transformations)


if __name__ == "__main__":

    import napari
    from multiview_stitcher.sample_data import get_mosaic_sample_data_path

    # filename = get_mosaic_sample_data_path()
    filename = "/Users/malbert/software/multiview-stitcher/image-datasets/arthur_20220621_premovie_dish2-max.czi"

    viewer = napari.Viewer()
    
    wdg = StitcherQWidget(viewer)
    viewer.window.add_dock_widget(wdg)

    viewer.open(filename, scene_index=0, plugin='napari-stitcher')

    wdg.button_load_layers_all.clicked()

    wdg.times_slider.value = (-1, 1)

    wdg.run_registration()
    # wdg.run_fusion()
