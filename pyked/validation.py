"""Validation class for ChemKED schema.
"""

# Python 2 compatibility
from __future__ import print_function
from __future__ import division

import sys
from warnings import warn
import re

import pkg_resources
import ruamel.yaml as yaml

import pint
from requests.exceptions import HTTPError, ConnectionError
from cerberus import Validator
import habanero
from orcid import SearchAPI

# Local imports
from .utils import units, Q_

if sys.version_info > (3,):
    long = int
    from functools import reduce

orcid_api = SearchAPI(sandbox=False)

# Load the ChemKED schema definition file
schema_file = pkg_resources.resource_filename(__name__, 'chemked_schema.yaml')
with open(schema_file, 'r') as f:
    schema = yaml.safe_load(f)

# These top-level keys in the schema serve as references for lower-level keys.
# They are removed to prevent conflicts due to required variables, etc.
for key in ['author', 'value-unit-required', 'value-unit-optional',
            'composition', 'ignition-type'
            ]:
    del schema[key]

# SI units for available value-type properties
property_units = {'temperature': 'kelvin',
                  'pressure': 'pascal',
                  'ignition-delay': 'second',
                  'pressure-rise': '1.0 / second',
                  'compression-time': 'second',
                  'volume': 'meter**3',
                  'time': 'second',
                  }


# Skip validation if True
_mod_skip_validation = False

def disable_validation():
    """Disable validation functions.
    """
    warn('validation disabled.')
    global _mod_skip_validation
    _mod_skip_validation = True


def enable_validation():
    """Enable validation functions.
    """
    warn('validation enabled.')
    global _mod_skip_validation
    _mod_skip_validation = False


def compare_name(given_name, family_name, question_name):
    """Compares a name in question to a specified name separated into given and family.

    The name in question ``question_name`` can be of varying format, including
    "Kyle E. Niemeyer", "Kyle Niemeyer", "K. E. Niemeyer", "KE Niemeyer", and
    "K Niemeyer". Other possibilities include names with hyphens such as
    "Chih-Jen Sung", "C. J. Sung", "C-J Sung".

    Examples:
        >>> compare_name('Kyle', 'Niemeyer', 'Kyle E Niemeyer')
        True
        >>> compare_name('Chih-Jen', 'Sung', 'C-J Sung')
        True

    Args:
        given_name (str): Given (or first) name to be checked against.
        family_name (str): Family (or last) name to be checked against.
        question_name (str): The whole name in question.

    Returns:
        bool: The return value. True for successful comparison, False otherwise.
    """
    # lowercase everything
    given_name = given_name.lower()
    family_name = family_name.lower()
    question_name = question_name.lower()

    # rearrange names given as "last, first middle"
    if ',' in question_name:
        name_split = question_name.split(',')
        name_split.reverse()
        question_name = ' '.join(name_split).strip()

    # split name in question by , <space> - .
    name_split = list(filter(None, re.split("[, \-.]+", question_name)))
    first_name = [name_split[0]]
    if len(name_split) == 3:
        first_name += [name_split[1]]

    given_name = list(filter(None, re.split("[, \-.]+", given_name)))

    if len(first_name) == 2 and len(given_name) == 2:
        # both have first and middle name/initial
        first_name[1] = first_name[1][0]
        given_name[1] = given_name[1][0]
    elif len(given_name) == 2 and len(first_name) == 1:
        del given_name[1]
    elif len(first_name) == 2 and len(given_name) == 1:
        del first_name[1]

    # first initial
    if len(first_name[0]) == 1 or len(given_name[0]) == 1:
        given_name[0] = given_name[0][0]
        first_name[0] = first_name[0][0]

    # first and middle initials combined
    if len(first_name[0]) == 2 or len(given_name[0]) == 2:
        given_name[0] = given_name[0][0]
        first_name[0] = name_split[0][0]

    return given_name == first_name and family_name == name_split[-1]


class OurValidator(Validator):
    """Custom validator with rules for Quantities and references.
    """
    def __init__(self, *args, **kwargs):
        """Initialization, mostly inherited from base class.
        """
        self._skip_validation = _mod_skip_validation
        super().__init__(*args, **kwargs)

    def disable_validation(self):
        """Disable validation functions.
        """
        warn('validation disabled.')
        self._skip_validation = True

    def enable_validation(self):
        """Enable validation functions.
        """
        warn('validation enabled.')
        self._skip_validation = False

    def _validate_isvalid_unit(self, isvalid_unit, field, value):
        """Checks for appropriate units using Pint unit registry.

        Args:
            isvalid_unit (bool): flag from schema indicating units to be checked.
            field (str): property associated with units in question.
            value (dict): dictionary of values from file associated with this property.
        """
        if isvalid_unit and not self._skip_validation:
            quantity = 1.0 * units(value['units'])
            try:
                quantity.to(property_units[field])
            except pint.DimensionalityError:
                self._error(field, 'incompatible units; should be consistent '
                            'with ' + property_units[field]
                            )

    def _validate_isvalid_quantity(self, isvalid_quantity, field, value):
        """Checks for valid given value and appropriate units.

        Args:
            isvalid_quantity (bool): flag from schema indicating quantity to be checked.
            field (str): property associated with quantity in question.
            value (str): string of the value of the quantity
        """
        if isvalid_quantity and not self._skip_validation:
            quantity = Q_(value)
            low_lim = 0.0 * units(property_units[field])

            try:
                if quantity <= low_lim:
                    self._error(
                        field, 'value must be greater than 0.0 {}'.format(property_units[field]),
                    )
            except pint.DimensionalityError:
                self._error(field, 'incompatible units; should be consistent '
                            'with ' + property_units[field]
                            )

    def _validate_isvalid_reference(self, isvalid_reference, field, value):
        """Checks valid reference metadata using DOI (if present).

        Todo:
            * remove UnboundLocalError from exception handling

        Args:
            isvalid_reference (bool): flag from schema indicating reference to be checked.
            field (str): 'reference'
            value (dict): dictionary of reference metadata.
        """
        if isvalid_reference and 'doi' in value and not self._skip_validation:
            try:
                ref = habanero.Crossref().works(ids=value['doi'])['message']
            except (HTTPError, habanero.RequestError):
                self._error(field, 'DOI not found')
                return
            # TODO: remove UnboundLocalError after habanero fixed
            except (ConnectionError, UnboundLocalError):
                warn('network not available, DOI not validated.')
                return

            # check journal name
            if ('journal' in value) and (value['journal'] not in ref['container-title']):
                self._error(field, 'journal does not match: ' +
                            ', '.join(ref['container-title'])
                            )
            # check year
            pub_year = (ref.get('published-print')
                        if 'published-print' in ref
                        else ref.get('published-online')
                        )['date-parts'][0][0]

            if ('year' in value) and (value['year'] != pub_year):
                self._error(field, 'year should be ' + str(pub_year))

            # check volume number
            if (('volume' in value) and ('volume' in ref) and
                    (value['volume'] != int(ref['volume']))):
                self._error(field, 'volume number should be ' + ref['volume'])

            # check pages
            if ('pages' in value) and ('page' in ref) and value['pages'] != ref['page']:
                self._error(field, 'pages should be ' + ref['page'])

            # check that all authors present
            authors = value['authors'][:]
            author_names = [a['name'] for a in authors]
            for author in ref['author']:
                # find using family name
                author_match = next(
                    (a for a in authors if
                     compare_name(author['given'], author['family'], a['name'])
                     ),
                    None
                    )
                # error if missing author in given reference information
                if author_match is None:
                    self._error(field, 'Missing author: ' +
                                ' '.join([author['given'], author['family']])
                                )
                else:
                    author_names.remove(author_match['name'])

                    # validate ORCID if given
                    orcid = author.get('ORCID')
                    if orcid:
                        # Crossref may give ORCID as http://orcid.org/####-####-####-####
                        # so need to strip the leading URL
                        orcid = orcid[orcid.rfind('/') + 1:]

                        if 'ORCID' in author_match:
                            if author_match['ORCID'] != orcid:
                                self._error(
                                    field, author_match['name'] + ' ORCID does ' +
                                    'not match that in reference. Reference: ' +
                                    orcid + '. Given: ' + author_match['ORCID']
                                    )
                        else:
                            # ORCID not given, suggest adding it
                            warn('ORCID ' + orcid + ' missing for ' + author_match['name'])

            # check for extra names given
            if len(author_names) > 0:
                self._error(field, 'Extra author(s) given: ' +
                            ', '.join(author_names)
                            )

    def _validate_isvalid_orcid(self, isvalid_orcid, field, value):
        """Checks for valid ORCID if given.

        Args:
            isvalid_orcid (bool): flag from schema indicating ORCID to be checked.
            field (str): 'author'
            value (dict): dictionary of author metadata.
        """
        if isvalid_orcid and 'ORCID' in value and not self._skip_validation:
            try:
                res = orcid_api.search_public('orcid:' + value['ORCID'])
            except ConnectionError:
                warn('network not available, ORCID not validated.')
                return

            # Return error if no results are found for the given ORCID
            if res['orcid-search-results']['num-found'] == 0:
                self._error(field, 'ORCID incorrect or invalid for ' +
                            value['name']
                            )
                return

            maplist = ['orcid-search-results', 'orcid-search-result', 0,
                       'orcid-profile', 'orcid-bio', 'personal-details',
                       'family-name', 'value'
                       ]
            family_name = reduce(lambda d, k: d[k], maplist, res)
            maplist[-2] = 'given-names'
            given_name = reduce(lambda d, k: d[k], maplist, res)

            if not compare_name(given_name, family_name, value['name']):
                self._error(field, 'Name and ORCID do not match. Name supplied: ' +
                            value['name'] + '. Name associated with ORCID: ' +
                            ' '.join([given_name, family_name])
                            )
