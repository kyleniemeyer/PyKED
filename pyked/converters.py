"""Module with converters from other formats.
"""

# Standard libraries
import os
from argparse import ArgumentParser
from warnings import warn
import numpy

from requests.exceptions import HTTPError, ConnectionError
import habanero
import pint

try:
    from lxml import etree
except ImportError:
    try:
        import xml.etree.ElementTree as etree
    except ImportError:
        print("Failed to import ElementTree from any known place")
        raise

# Local imports
from .validation import yaml, property_units
from .chemked import ChemKED, DataPoint
from .utils import units as unit_registry
from ._version import __version__


# Valid properties for ReSpecTh dataGroup
datagroup_properties = ['temperature', 'pressure', 'ignition delay', 'pressure rise']

# Exceptions
class ParseError(Exception):
    """Base class for errors."""
    pass

class KeywordError(ParseError):
    """Raised for errors in keyword parsing."""

    def __init__(self, *keywords):
        self.keywords = keywords

    def __str__(self):
        return repr('Error: {}.'.format(self.keywords[0]))

class MissingElementError(KeywordError):
    """Raised for missing required elements."""

    def __str__(self):
        return repr('Error: required element {} is missing.'.format(
            self.keywords[0]))

class MissingAttributeError(KeywordError):
    """Raised for missing required attribute."""

    def __str__(self):
        return repr('Error: required attribute {} of {} is missing.'.format(
            self.keywords[0], self.keywords[1])
            )

class UndefinedKeywordError(KeywordError):
    """Raised for undefined keywords."""

    def __str__(self):
        return repr('Error: keyword not defined: {}'.format(self.keywords[0]))


def get_file_metadata(root):
    """Read and parse ReSpecTh XML file metadata (file author, version, etc.)

    Args:
        root (`etree.Element`): Root of ReSpecTh XML file

    Returns:
        properties (`dict`): Dictionary with file metadata
    """
    properties = {}

    properties['file-author'] = {'name': ''}
    try:
        properties['file-author']['name'] = root.find('fileAuthor').text
    except AttributeError:
        raise MissingElementError('fileAuthor')

    if properties['file-author']['name'] == '':
        raise MissingElementError('fileAuthor')

    # Default version is 0 for the ChemKED file
    properties['file-version'] = 0

    # Default ChemKED version
    properties['chemked-version'] = __version__

    return properties


def get_reference(root):
    """Read reference info from root of ReSpecTh XML file.

    Args:
        root (`etree.Element`): Root of ReSpecTh XML file

    Returns:
        properties (`dict`): Dictionary with reference information
    """
    reference = {}
    elem = root.find('bibliographyLink')
    if elem is None:
        raise MissingElementError('bibliographyLink')

    # Try to get reference info via DOI
    try:
        reference['doi'] = elem.attrib['doi']
        ref = None
        try:
            ref = habanero.Crossref().works(ids=reference['doi'])['message']
        except (HTTPError, habanero.RequestError):
            print('DOI not found')
            raise KeyError
        # TODO: remove UnboundLocalError after habanero fixed
        except (ConnectionError, UnboundLocalError):
            warn('network not available, DOI not validated.')
            raise KeyError

        if ref is not None:
            ## Now get elements of the reference data
            # Assume that the reference returned by the DOI lookup always has a container-title
            reference['journal'] = ref.get('container-title')[0]
            ref_year = ref.get('published-print') or ref.get('published-online')
            reference['year'] = int(ref_year['date-parts'][0][0])
            reference['volume'] = int(ref.get('volume'))
            reference['pages'] = ref.get('page')
            reference['authors'] = []
            for author in ref['author']:
                auth = {}
                auth['name'] = ' '.join([author['given'], author['family']])
                # Add ORCID if available
                orcid = author.get('ORCID')
                if orcid:
                    auth['ORCID'] = orcid
                reference['authors'].append(auth)

    except KeyError:
        print('Warning: missing doi attribute in bibliographyLink')
        print('Setting "detail" key as a fallback; please update.')
        try:
            reference['detail'] = elem.attrib['preferredKey']
            if reference['detail'][-1] != '.':
                reference['detail'] += '.'
        except KeyError:
            # Need one of DOI or preferredKey
            raise MissingAttributeError('preferredKey', 'bibliographyLink')

    return reference


def get_experiment_kind(root):
    """Read common properties from root of ReSpecTh XML file.

    Args:
        root (`etree.Element`): Root of ReSpecTh XML file

    Returns:
        properties (`dict`): Dictionary with experiment type and apparatus information.
    """
    properties = {}
    if root.find('experimentType').text == 'Ignition delay measurement':
        properties['experiment-type'] = 'ignition delay'
    else:
        #TODO: support additional experimentTypes
        raise NotImplementedError(root.find('experimentType').text + ' not (yet) supported')

    properties['apparatus'] = {'kind': '', 'institution': '', 'facility': ''}
    try:
        kind = root.find('apparatus/kind').text
    except:
        raise MissingElementError('apparatus/kind')
    if kind in ['shock tube', 'rapid compression machine']:
        properties['apparatus']['kind'] = kind
    else:
        raise NotImplementedError(kind + ' experiment not (yet) supported')

    return properties


def get_common_properties(root):
    """Read common properties from root of ReSpecTh XML file.

    Args:
        root (`etree.Element`): Root of ReSpecTh XML file

    Returns:
        properties (`dict`): Dictionary with common properties
    """
    properties = {}

    for elem in root.iterfind('commonProperties/property'):
        name = elem.attrib['name']

        if name == 'initial composition':
            properties['composition'] = {'species': []}
            composition_type = None

            for child in elem.iter('component'):
                spec = {}
                spec['species-name'] = child.find('speciesLink').attrib['preferredKey']

                # use InChI for unique species identifier (if present)
                try:
                    spec['InChI'] = child.find('speciesLink').attrib['InChI']
                except KeyError:
                    # TODO: add InChI validator/search
                    print('Warning: missing InChI for species ' + spec['species-name'])
                    pass

                # amount of that species
                spec['amount'] = [float(child.find('amount').text)]

                properties['composition']['species'].append(spec)

                # check consistency of composition type
                if not composition_type:
                    composition_type = child.find('amount').attrib['units']
                elif composition_type != child.find('amount').attrib['units']:
                    raise KeywordError('inconsistent initial composition units')
            assert composition_type in ['mole fraction', 'mass fraction']
            properties['composition']['kind'] = composition_type

        elif name in ['temperature', 'pressure', 'pressure rise', 'compression time']:
            field = name.replace(' ', '-')
            units = elem.attrib['units']
            if units == 'Torr':
                units = 'torr'
            quantity = 1.0 * unit_registry(units)
            try:
                quantity.to(property_units[field])
            except pint.DimensionalityError:
                raise KeywordError('units incompatible for property ' + name)

            properties[field] = [' '.join([elem.find('value').text, units])]

        else:
            raise KeywordError('Property ' + name + ' not supported as common property.')

    return properties


def get_ignition_type(root):
    """Gets ignition type and target.

    Args:
        root (`etree.Element`): Root of ReSpecTh XML file

    Returns:
        properties (`dict`): Dictionary with ignition type/target information
    """
    ignition = {}
    elem = root.find('ignitionType')

    if elem is None:
        raise MissingElementError('ignitionType')

    try:
        ign_target = elem.attrib['target'].rstrip(';').upper()
    except KeyError:
        raise MissingAttributeError('target', 'ignitionType')
    try:
        ign_type = elem.attrib['type']
    except KeyError:
        raise MissingAttributeError('type', 'ignitionType')

    # ReSpecTh allows multiple ignition targets
    if len(ign_target.split(';')) > 1:
        raise NotImplementedError('Multiple ignition targets not supported.')

    # Acceptable ignition targets include pressure, temperature, and species
    # concentrations
    if ign_target == 'OHEX':
        ign_target = 'OH*'
    elif ign_target == 'CHEX':
        ign_target = 'CH*'

    if ign_target not in ['P', 'T', 'OH', 'OH*', 'CH*', 'CH']:
        raise KeywordError(ign_target + ' not valid ignition target')

    if ign_type not in ['max', 'd/dt max', '1/2 max', 'min']:
        raise KeywordError(ign_type + ' not valid ignition type')

    if ign_target == 'P':
        ign_target = 'pressure'
    elif ign_target == 'T':
        ign_target = 'temperature'

    ignition['type'] = ign_type
    ignition['target'] = ign_target

    return ignition


def get_datapoints(root):
    """Parse datapoints with ignition delay from file.

    Args:
        root (`etree.Element`): Root of ReSpecTh XML file

    Returns:
        properties (`dict`): Dictionary with ignition delay data
    """
    # Shock tube experiment will have one data group, while RCM may have one
    # or two (one for ignition delay, one for volume-history)
    dataGroups = root.findall('dataGroup')
    if not dataGroups:
        raise MissingElementError('dataGroup')

    # all situations will have main experimental data in first dataGroup
    dataGroup = dataGroups[0]
    property_id = {}
    unit_id = {}
    # get properties of dataGroup
    for prop in dataGroup.findall('property'):
        unit_id[prop.attrib['id']] = prop.attrib['units']
        property_id[prop.attrib['id']] = prop.attrib['name']
        if property_id[prop.attrib['id']] not in datagroup_properties:
            raise KeyError(property_id[prop.attrib['id']] + ' not valid dataPoint property')
    if not property_id:
        raise MissingElementError('property')

    # now get data points
    datapoints = []
    for dp in dataGroup.findall('dataPoint'):
        datapoint = {}
        for val in dp:
            units = unit_id[val.tag]
            if units == 'Torr':
                units = 'torr'
            datapoint[property_id[val.tag].replace(' ', '-')] = [val.text + ' ' + units]
        datapoints.append(datapoint)

    if len(datapoints) == 0:
        raise MissingElementError('dataPoint')

    # RCM files may have a second dataGroup with volume-time history
    if len(dataGroups) == 2:
        assert root.find('apparatus/kind').text == 'rapid compression machine', \
               'Second dataGroup only valid for RCM.'

        assert len(datapoints) == 1, 'Multiple datapoints for single volume history.'

        dataGroup = dataGroups[1]
        for prop in dataGroup.findall('property'):
            if prop.attrib['name'] == 'time':
                time_dict = {'units': prop.attrib['units'], 'column': 0}
                time_tag = prop.attrib['id']
            elif prop.attrib['name'] == 'volume':
                volume_dict = {'units': prop.attrib['units'], 'column': 1}
                volume_tag = prop.attrib['id']

        volume_history = {'time': time_dict, 'volume': volume_dict, 'values': []}

        # collect volume-time history
        for dp in dataGroup.findall('dataPoint'):
            time = None
            volume = None
            for val in dp:
                if val.tag == time_tag:
                    time = float(val.text)
                elif val.tag == volume_tag:
                    volume = float(val.text)
            volume_history['values'].append([time, volume])

        datapoints[0]['volume-history'] = volume_history

    elif len(dataGroups) > 2:
        raise NotImplementedError('More than two DataGroups not supported.')

    return datapoints


def convert_ReSpecTh(filename_xml, output='', file_author='', file_author_orcid=''):
    """Convert ReSpecTh XML file to ChemKED YAML file.

    Args:
        filename_xml (`str`): Name of ReSpecTh XML file to be converted.
        output (`str`, optional): Output path for converted file.
        file_author (`str`, optional): Name to override original file author
        file_author_orcid (`str`, optional): ORCID of file author

    Returns:
        filename_yaml (`str`): Name of newly created ChemKED YAML file.
    """
    assert os.path.isfile(filename_xml), 'Error: ' + filename_xml + ' file missing'

    # get all information from XML file
    try:
        tree = etree.parse(filename_xml)
    except OSError:
        raise OSError('Unable to open file ' + filename_xml)
    root = tree.getroot()

    # get file metadata
    properties = get_file_metadata(root)

    # get reference info
    properties['reference'] = get_reference(root)
    # Save name of original data filename
    if properties['reference'].get('detail') is None:
        properties['reference']['detail'] = ''
    properties['reference']['detail'] += ('Converted from XML file ' +
                                          os.path.basename(filename_xml)
                                          )

    # Ensure ignition delay, and get which kind of experiment
    properties.update(get_experiment_kind(root))

    # Get properties shared across the file
    properties['common-properties'] = get_common_properties(root)

    # Determine definition of ignition delay
    properties['common-properties']['ignition-type'] = get_ignition_type(root)

    # Now parse ignition delay datapoints
    properties['datapoints'] = get_datapoints(root)

    # Get compression time for RCM, if volume history given
    # if 'volume' in properties and 'compression-time' not in properties:
    #     min_volume_idx = numpy.argmin(properties['volume'])
    #     min_volume_time = properties['time'][min_volume_idx]
    #     properties['compression-time'] = min_volume_time

    # Ensure combinations of volume, time, pressure-rise are correct.
    if ('volume' in properties['common-properties'] and
        'time' not in properties['common-properties']
        ):
        raise KeywordError('Time values needed for volume history')
    elif (any(['volume' in dp for dp in properties['datapoints']]) and
          not any(['time' in dp for dp in properties['datapoints']])
          ):
        raise KeywordError('Time values needed for volume history')

    if ('compression-time' in properties['common-properties'] or
        any([dp for dp in properties['datapoints'] if dp.get('compression-time')])
        ) and properties['apparatus']['kind'] == 'shock tube':
        raise KeywordError('Compression time cannot be defined for shock tube.')

    if ('pressure-rise' in properties['common-properties'] or
        any([dp for dp in properties['datapoints'] if dp.get('pressure-rise')])
        ) and properties['apparatus']['kind'] == 'rapid compression machine':
        raise KeywordError('Pressure rise cannot be defined for RCM.')

    if (('volume' in properties['common-properties'] and
         'pressure-rise' in properties['common-properties']
         ) or ('volume' in properties['common-properties'] and
               any([dp for dp in properties['datapoints'] if dp.get('pressure-rise')])
               ) or ('pressure-rise' in properties['common-properties'] and
                     any([dp for dp in properties['datapoints'] if dp.get('volume')])
                     )
        ):
        raise KeywordError('Both volume history and pressure rise '
                           'cannot be specified'
                           )

    # apply any overrides
    if file_author:
        properties['file-author']['name'] = file_author
    if file_author_orcid:
        properties['file-author']['ORCID'] = file_author_orcid

    # Now go through datapoints and apply common properties
    for idx in range(len(properties['datapoints'])):
        for prop in properties['common-properties']:
            properties['datapoints'][idx][prop] = properties['common-properties'][prop]

    # compression time doesn't belong in common-properties
    properties['common-properties'].pop('compression-time', None)

    filename_yaml = os.path.splitext(os.path.basename(filename_xml))[0] + '.yaml'

    # add path
    filename_yaml = os.path.join(output, filename_yaml)

    with open(filename_yaml, 'w') as outfile:
        outfile.write(yaml.dump(properties, default_flow_style=False))
    print('Converted to ' + filename_yaml)

    # now validate
    ChemKED(yaml_file=filename_yaml)

    return filename_yaml


def convert_to_ReSpecTh(filename_ck, output_path=''):
    """Convert ChemKED file to ReSpecTh XML file.

    This converter uses common information in a ChemKED file to generate a
    ReSpecTh XML file. Note that some information may be lost, as ChemKED stores
    some additional attributes.

    Arguments:
        filename_ck (`str`): Filename of existing ChemKED YAML file to be converted.
        output_path (`str`, optional): Path for output ReSpecTh XML file.
    """
    c = ChemKED(yaml_file=filename_ck)

    root = etree.Element('experiment')

    file_author = etree.SubElement(root, 'fileAuthor')
    file_author.text = c.file_author['name']

    # right now ChemKED just uses an integer file version
    file_version = etree.SubElement(root, 'fileVersion')
    major_version = etree.SubElement(file_version, 'major')
    major_version.text = str(c.file_version)
    minor_version = etree.SubElement(file_version, 'minor')
    minor_version.text = '0'

    respecth_version = etree.SubElement(root, 'ReSpecThVersion')
    major_version = etree.SubElement(respecth_version, 'major')
    major_version.text = '1'
    minor_version = etree.SubElement(respecth_version, 'minor')
    minor_version.text = '0'

    # Only ignition delay currently supported
    exp = etree.SubElement(root, 'experimentType')
    if c.experiment_type == 'ignition delay':
        exp.text = 'Ignition delay measurement'
    else:
        raise NotImplementedError('Only ignition delay type supported for conversion.')

    reference = etree.SubElement(root, 'bibliographyLink')
    citation = ''
    for author in c.reference.authors:
        citation += author['name'] + ', '
    citation += (c.reference.journal + ' (' + str(c.reference.year) + ') ' +
                 str(c.reference.volume) + ':' + c.reference.pages + '. ' + c.reference.detail
                 )
    reference.set('preferredKey', citation)
    reference.set('doi', c.reference.doi)

    apparatus = etree.SubElement(root, 'apparatus')
    kind = etree.SubElement(apparatus, 'kind')
    kind.text = c.apparatus.kind

    common_properties = etree.SubElement(root, 'commonProperties')
    # ChemKED objects have no common properties once loaded... can we check for properties
    # among datapoints that tend to be common?
    common = []
    composition = c.datapoints[0].composition
    # Composition type *has* to be the same
    composition_type = c.datapoints[0].composition_type
    if all([composition == dp.composition for dp in c.datapoints]):
        # initial composition is common
        common.append('composition')
        prop = etree.SubElement(common_properties, 'property')
        prop.set('name', 'initial composition')

        for species in composition:
            component = etree.SubElement(prop, 'component')
            species_link = etree.SubElement(component, 'speciesLink')
            species_link.set('preferredKey', species['species-name'])
            if species.get('InChI'):
                species_link.set('InChI', species['InChI'])

            amount = etree.SubElement(component, 'amount')
            amount.set('units', composition_type)
            amount.text = str(species['amount'].magnitude)

    # If multiple datapoints present, then find any common properties. If only
    # one datapoint, then composition should be the only "common" property.
    if len(c.datapoints) > 1:
        for prop_name in datagroup_properties:
            attribute = prop_name.replace(' ', '_')
            quantity = getattr(c.datapoints[0], attribute)
            if (quantity != None and
                all([quantity == getattr(dp, attribute) for dp in c.datapoints])
                ):
                common.append(prop_name)
                prop = etree.SubElement(common_properties, 'property')
                prop.set('description', '')
                prop.set('name', prop_name)
                prop.set('units', str(quantity.units))

                value = etree.SubElement(prop, 'value')
                value.text = str(quantity.magnitude)

    # Ignition delay can't be common, unless only a single datapoint.

    datagroup = etree.SubElement(root, 'dataGroup')
    datagroup.set('id', 'dg1')
    datagroup_link = etree.SubElement(datagroup, 'dataGroupLink')
    datagroup_link.set('dataGroupID', '')
    datagroup_link.set('dataPointID', '')

    property_idx = {}
    labels = {'temperature': 'T', 'pressure': 'P',
              'ignition delay': 'tau', 'pressure rise': 'dP/dt',
              }

    for prop_name in ['temperature', 'pressure', 'ignition delay', 'pressure rise']:
        attribute = prop_name.replace(' ', '_')
        if (prop_name not in common and
            any([getattr(dp, attribute, None) for dp in c.datapoints])
            ):
            prop = etree.SubElement(datagroup, 'property')
            prop.set('description', '')
            prop.set('name', prop_name)
            prop.set('units', str(getattr(c.datapoints[0], attribute).units))
            idx = 'x{}'.format(len(property_idx) + 1)
            property_idx[idx] = prop_name
            prop.set('id', idx)
            prop.set('label', labels[prop_name])

    if 'composition' not in common:
        for species in c.datapoints[0].composition:
            prop = etree.SubElement(datagroup, 'property')
            prop.set('description', '')

            idx = 'x{}'.format(len(property_idx) + 1)
            property_idx[idx] = species['species-name']
            prop.set('id', idx)
            prop.set('label', '[' + species['species-name'] + ']')
            prop.set('name', 'composition')
            prop.set('units', c.datapoints[0].composition_type)

            species_link = etree.SubElement(prop, 'speciesLink')
            species_link.set('preferredKey', species['species-name'])
            if species.get('InChI'):
                species_link.set('InChI', species['InChI'])

    for dp in c.datapoints:
        datapoint = etree.SubElement(datagroup, 'dataPoint')
        for idx in property_idx:
            value = etree.SubElement(datapoint, idx)
            value.text = str(getattr(dp, property_idx[idx].replace(' ', '_')).magnitude)

    # if RCM and has volume history, need a second dataGroup
    if (len(c.datapoints) > 1 and
        any([getattr(dp, 'volume_history', None) for dp in c.datapoints])
        ):
        raise NotImplementedError('Error: ReSpecTh files do not support multiple datapoints '
                                  'with a volume history.'
                                  )
        # TODO: what if they share the same history? Does this happen?
    elif getattr(c.datapoints[0], 'volume_history', None):
        datagroup = etree.SubElement(root, 'dataGroup')
        datagroup.set('id', 'dg1')
        datagroup_link = etree.SubElement(datagroup, 'dataGroupLink')
        datagroup_link.set('dataGroupID', '')
        datagroup_link.set('dataPointID', '')

        # Volume history has two properties: time and volume.
        volume_history = c.datapoints[0].volume_history
        prop = etree.SubElement(datagroup, 'property')
        prop.set('description', '')
        prop.set('name', 'time')
        prop.set('units', str(volume_history.time.units))
        time_idx = 'x{}'.format(len(property_idx) + 1)
        prop.set('id', time_idx)
        prop.set('label', 't')

        prop = etree.SubElement(datagroup, 'property')
        prop.set('description', '')
        prop.set('name', 'volume')
        prop.set('units', str(volume_history.volume.units))
        volume_idx = 'x{}'.format(len(property_idx) + 2)
        prop.set('id', volume_idx)
        prop.set('label', 'V')

        for time, volume in zip(volume_history.time, volume_history.volume):
            datapoint = etree.SubElement(datagroup, 'dataPoint')
            value = etree.SubElement(datapoint, time_idx)
            value.text = str(time.magnitude)
            value = etree.SubElement(datapoint, volume_idx)
            value.text = str(volume.magnitude)

    # In ReSpecTh files all datapoints share ignition type
    ignition = etree.SubElement(root, 'ignitionType')
    if c.datapoints[0].ignition_type['target'] == 'pressure':
        ignition.set('target', 'P')
    elif c.datapoints[0].ignition_type['target'] == 'temperature':
        ignition.set('target', 'T')
    else:
        # options left are species
        ignition.set('target', c.datapoints[0].ignition_type['target'])
    ignition.set('type', c.datapoints[0].ignition_type['type'])

    et = etree.ElementTree(root)
    filename_out = os.path.join(
        output_path,
        os.path.splitext(os.path.basename(filename_ck))[0] + '.xml'
        )
    et.write(filename_out, pretty_print=True, encoding='utf-8', xml_declaration=True)

    return filename_out


if __name__ == '__main__':
    parser = ArgumentParser(description='Convert ReSpecTh XML file to ChemKED '
                                        'YAML file.'
                            )
    parser.add_argument('-i', '--input',
                        type=str,
                        required=True,
                        help='Input XML filename'
                        )
    parser.add_argument('-o', '--output',
                        type=str,
                        required=False,
                        default='',
                        help='Output directory for file'
                        )
    parser.add_argument('-fa', '--file-author',
                        dest='file_author',
                        type=str,
                        required=False,
                        default='',
                        help='File author name to override original'
                        )
    parser.add_argument('-fo', '--file-author-orcid',
                        dest='file_author_orcid',
                        type=str,
                        required=False,
                        default='',
                        help='File author ORCID'
                        )

    args = parser.parse_args()
    convert_ReSpecTh(args.input, args.output,
                     args.file_author, args.file_author_orcid
                     )
