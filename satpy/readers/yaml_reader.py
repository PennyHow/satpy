#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright (c) 2016.

# Author(s):

#   Martin Raspaud <martin.raspaud@smhi.se>

# This file is part of satpy.

# satpy is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.

# satpy is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE.  See the GNU General Public License for more details.

# You should have received a copy of the GNU General Public License along with
# satpy.  If not, see <http://www.gnu.org/licenses/>.

# New stuff

import glob
import logging
import numbers
import os
from fnmatch import fnmatch

import six
import yaml
import numpy as np
from satpy.readers import DatasetID, DatasetDict
from satpy.projectable import Projectable, combine_info
from trollsift.parser import globify, parse
from pyresample import geometry

LOG = logging.getLogger(__name__)


class YAMLBasedReader(object):

    def __init__(self, config_files):
        self.config = {}
        self.config_files = config_files
        for config_file in config_files:
            with open(config_file) as fd:
                self.config.update(yaml.load(fd))
        self.datasets = self.config['datasets']
        self.info = self.config['reader']
        self.name = self.info['name']
        self.sensor_names = self.info['sensors']

        self.info['filenames'] = []
        self.file_patterns = []
        for file_type in self.config['file_types'].values():
            self.file_patterns.extend(file_type['file_patterns'])
        self.file_handlers = {}
        self.ids = {}
        self.get_dataset_ids()

    @property
    def start_time(self):
        if not self.file_handlers:
            raise RuntimeError("Start time unknown until files are selected")
        return min(x.start_time() for x in self.file_handlers.values()[0])

    @property
    def end_time(self):
        if not self.file_handlers:
            raise RuntimeError("End time unknown until files are selected")
        return max(x.end_time() for x in self.file_handlers.values()[0])

    def select_files(self, base_dir=None, filenames=None, sensor=None, start_time=None, end_time=None, area=None):
        if isinstance(sensor, (str, six.text_type)):
            sensor_set = set([sensor])
        elif sensor is not None:
            sensor_set = set(sensor)
        else:
            sensor_set = set()

        if sensor is not None and not (set(self.info.get("sensors")) & sensor_set):
            return filenames, []

        file_set = set()
        if filenames:
            file_set |= set(filenames)

        if filenames is None:
            self.info["filenames"] = self.find_filenames(base_dir)
        else:
            self.info["filenames"] = self.match_filenames(filenames, base_dir)
        if not self.info["filenames"]:
            LOG.warning("No filenames found for reader: %s", self.name)
        file_set -= set(self.info['filenames'])
        LOG.debug("Assigned to %s: %s", self.info['name'], self.info['filenames'])

        # Organize filenames in to file types and create file handlers
        remaining_filenames = set(self.info['filenames'])
        for filetype, filetype_info in self.config['file_types'].items():
            filetype_cls = filetype_info['file_reader']
            patterns = filetype_info['file_patterns']
            self.file_handlers[filetype] = []
            for pattern in patterns:
                used_filenames = set()
                for filename in remaining_filenames:
                    if fnmatch(os.path.basename(filename), globify(pattern)):
                        # we know how to use this file (even if we may not use it later)
                        used_filenames.add(filename)
                        filename_info = parse(pattern, os.path.basename(filename))
                        file_handler = filetype_cls(filename, filename_info, **filetype_info)

                        # Only add this file handler if it is within the time we want
                        if start_time and file_handler.start_time() < start_time:
                            continue
                        if end_time and file_handler.end_time() > end_time:
                            continue

                        # TODO: Area filtering

                        self.file_handlers[filetype].append(file_handler)
                remaining_filenames -= used_filenames

            # Sort the file handlers by start time
            self.file_handlers[filetype].sort(key=lambda fh: fh.start_time())

        return file_set, self.info['filenames']

    def match_filenames(self, filenames, base_dir=None):
        result = []
        for file_pattern in self.file_patterns:
            if base_dir is not None:
                file_pattern = os.path.join(base_dir, file_pattern)
            pattern = globify(file_pattern)
            if not filenames:
                return result
            for filename in list(filenames):
                if fnmatch(os.path.basename(filename), os.path.basename(pattern)):
                    result.append(filename)
                    filenames.remove(filename)
        return result

    def find_filenames(self, directory, file_patterns=None):
        if file_patterns is None:
            file_patterns = self.file_patterns
            # file_patterns.extend(item['file_patterns'] for item in self.config['file_types'])
        filelist = []
        for pattern in file_patterns:
            filelist.extend(glob.iglob(os.path.join(directory, globify(pattern))))
        return filelist

    def get_dataset_ids(self):
        """Get the dataset ids from the config."""
        ids = []
        for dskey, dataset in self.datasets.items():
            nb_ids = 1
            for key, val in dataset.items():
                if not key.endswith('range') and isinstance(val, (list, tuple, set)):
                    nb_ids = len(val)
                    break
            for idx in range(nb_ids):
                kwargs = {'name': dataset.get('name'),
                          'wavelength': tuple(dataset.get('wavelength_range'))}
                for key in ['resolution', 'calibration', 'polarization']:
                    kwargs[key] = dataset.get(key)
                    if isinstance(kwargs[key], (list, tuple, set)):
                        kwargs[key] = kwargs[key][idx]
                dsid = DatasetID(**kwargs)
                ids.append(dsid)
                self.ids[dsid] = dskey, dataset
        return ids

    def _load_dataset(self, file_handlers, dsid, ds_info):
        # TODO: Put this in it's own function probably
        try:
            # Can we allow the file handlers to do inplace data writes?
            all_shapes = [fh.get_shape(dsid, **ds_info) for fh in file_handlers]
            # rows accumlate, columns stay the same
            overall_shape = (sum([x[0] for x in all_shapes]), all_shapes[0][1])
        except (NotImplementedError, Exception):
            # FIXME: Is NotImplementedError included in Exception for all versions of Python?
            all_shapes = None
            overall_shape = None

        if overall_shape is None:
            # can't optimize by using inplace loading
            projectables = []
            for fh in file_handlers:
                projectables.append(fh.get_dataset(dsid, ds_info))

            # Join them all together
            all_shapes = [x.shape for x in projectables]
            combined_info = file_handlers[0].combine_info([p.info for p in projectables])
            proj = Projectable(np.vstack(projectables), **combined_info)
            del projectables  # clean up some space since we don't need these anymore
        else:
            # we can optimize
            # create a projectable object for the file handler to fill in
            proj = Projectable(np.empty(overall_shape, dtype=ds_info.get('dtype', np.float32)))

            offset = 0
            for idx, fh in enumerate(file_handlers):
                granule_height = all_shapes[idx][0]
                # XXX: Does this work with masked arrays and subclasses of them?
                # Otherwise, have to send in separate data, mask, and info parameters to be filled in
                # TODO: Combine info in a sane way
                fh.get_dataset(dsid, ds_info, out=proj[offset: offset + granule_height])
                offset += granule_height

        # Update the metadata
        proj.info['start_time'] = file_handlers[0].start_time()
        proj.info['end_time'] = file_handlers[-1].end_time()

        return all_shapes, proj

    def _load_area(self, file_handlers, nav_name, nav_info, all_shapes, shape):
        lons = np.ma.empty(shape, dtype=nav_info.get('dtype', np.float32))
        lats = np.ma.empty(shape, dtype=nav_info.get('dtype', np.float32))
        offset = 0
        for idx, fh in enumerate(file_handlers):
            granule_height = all_shapes[idx][0]
            fh.get_area(nav_name, nav_info,
                         lon_out=lons[offset: offset + granule_height],
                         lat_out=lats[offset: offset + granule_height])
            offset += granule_height

        area = geometry.SwathDefinition(lons, lats)
        # FIXME: How do we name areas?
        area.name = nav_name
        return area

    def load(self, dataset_keys, area=None, start_time=None, end_time=None, **kwargs):
        loaded_navs = {}
        datasets = DatasetDict()
        for dataset_key in dataset_keys:
            dsid = self.get_dataset_key(dataset_key)
            ds_info = self.ids[dsid][1]
            # HACK: wavelength is a list from pyyaml but a tuple in the dsid
            # when setting it in the DatasetDict it will fail because they are not equal
            if "wavelength_range" in ds_info:
                ds_info["wavelength_range"] = tuple(ds_info["wavelength_range"])

            # Get the file handler to load this dataset
            filetype = ds_info['file_type']
            if filetype not in self.file_handlers:
                raise RuntimeError("Required file type '{}' not found or loaded".format(filetype))
            file_handlers = self.file_handlers[filetype]

            all_shapes, proj = self._load_dataset(file_handlers, dsid, ds_info)
            datasets[dsid] = proj

            if 'area' not in proj.info or proj.info['area'] is None:
                # we need to load the area because the file handlers didn't
                nav_name = ds_info.get('navigation')
                if nav_name is None or nav_name not in self.config['navigation']:
                    # we don't know how to load navigation
                    LOG.warning("Can't load navigation for {}".format(dsid))
                elif nav_name in loaded_navs:
                    ds_area = loaded_navs[nav_name]
                else:
                    nav_info = self.config['navigation'][nav_name]
                    nav_filetype = nav_info['file_type']
                    if nav_filetype not in self.file_handlers:
                        raise RuntimeError("Required file type '{}' not found or loaded".format(nav_filetype))
                    nav_fhs = self.file_handlers[nav_filetype]

                    ds_area = self._load_area(nav_fhs, nav_name, nav_info, all_shapes, proj.shape)
                    loaded_navs[nav_name] = ds_area
                proj.info["area"] = ds_area

        return datasets

    def get_dataset_key(self, key, calibration=None, resolution=None, polarization=None, aslist=False):
        """Get the fully qualified dataset corresponding to *key*, either by name or centerwavelength.

        If `key` is a `DatasetID` object its name is searched if it exists, otherwise its wavelength is used.
        """
        # TODO This can be made simpler
        # get by wavelength
        if isinstance(key, numbers.Number):
            datasets = [ds for ds in self.ids if ds.wavelength and (ds.wavelength[0] <= key <= ds.wavelength[2])]
            datasets = sorted(datasets, key=lambda ch: abs(ch.wavelength[1] - key))
            if not datasets:
                raise KeyError("Can't find any projectable at %gum" % key)
        elif isinstance(key, DatasetID):
            if key.name is not None:
                datasets = self.get_dataset_key(key.name, aslist=True)
            elif key.wavelength is not None:
                datasets = self.get_dataset_key(key.wavelength, aslist=True)
            else:
                raise KeyError("Can't find any projectable '{}'".format(key))

            if calibration is None and key.calibration is not None:
                calibration = [key.calibration]
            if resolution is None and key.resolution is not None:
                resolution = [key.resolution]
            if polarization is None and key.polarization is not None:
                polarization = [key.polarization]
        # get by name
        else:
            datasets = [ds_id for ds_id in self.ids if ds_id.name == key]
            if not datasets:
                raise KeyError("Can't find any projectable called '{}'".format(key))
        # default calibration choices
        if calibration is None:
            calibration = ["brightness_temperature", "reflectance"]

        if resolution is not None:
            if not isinstance(resolution, (tuple, list, set)):
                resolution = [resolution]
            datasets = [ds_id for ds_id in datasets if ds_id.resolution in resolution]
        if calibration is not None:
            # order calibration from highest level to lowest level
            calibration = [x for x in ["brightness_temperature",
                                       "reflectance", "radiance", "counts"] if x in calibration]
            datasets = [ds_id for ds_id in datasets if ds_id.calibration is None or ds_id.calibration in calibration]
        if polarization is not None:
            datasets = [ds_id for ds_id in datasets if ds_id.polarization in polarization]

        if not datasets:
            raise KeyError("Can't find any projectable matching '{}'".format(str(key)))
        if aslist:
            return datasets
        else:
            return datasets[0]


def match_file_names_and_types(filenames, filetypes):
    res = {}
    for filename in filenames:
        for filetype, patterns in filetypes.items():
            for pattern in patterns:
                if fnmatch(os.path.basename(filename), globify(pattern)):
                    res.setdefault(filetype, []).append((filename, parse(pattern, os.path.basename(filename))))
                    break
            if filename in res:
                break
    return res


def my_vstack(arrays):
    return arrays[0]

# what about the metadata ?


def multiload(datasets, area, start_time, end_time):
    files = {}
    # select files to use
    trimmed_datasets = datasets.copy()
    first_start_time = None
    last_end_time = None
    for dataset, fhds in trimmed_datasets.items():
        for fhd in reversed(fhds):
            remove = False
            if start_time and fhd.start_time() < start_time:
                remove = True
            else:
                if first_start_time is None or first_start_time > fhd.start_time():
                    first_start_time = fhd.start_time()
            if end_time and fhd.end_time() > fhd.end_time():
                remove = True
            else:
                if last_end_time is None or last_end_time < fhd.end_time():
                    last_end_time = fhd.end_time()
            # TODO: area-based selection
            if remove:
                fhds.remove(fhd)
            else:
                files.setdefault(fhd, []).append(dataset)

    res = []
    for fhd, datasets in files.items():
        res.extend(fhd.load(datasets))

    # sort datasets by ds_id and time

    datasets = {}
    for dataset in res:
        datasets.setdefault(dataset.info['id'], []).append(dataset)

    # FIXME: stacks should end up being datasets.
    res = dict([(key, my_vstack(dss)) for key, dss in datasets.items()])

    # for dataset in res:
#        dataset.info['start_time'] = first_start_time
#        dataset.info['end_time'] = last_end_time

    # TODO: take care of the metadata

    return res