"""
Rules for data collection
"""
from __future__ import absolute_import
import hashlib
import json
import logging
import six
import shlex
import os
import requests
import yaml
import stat
from six.moves import configparser as ConfigParser

from subprocess import Popen, PIPE, STDOUT
from tempfile import NamedTemporaryFile
from .constants import InsightsConstants as constants

APP_NAME = constants.app_name
logger = logging.getLogger(__name__)
NETWORK = constants.custom_network_log_level

expected_keys = ('commands', 'files', 'patterns', 'keywords')


def resolve(d):
    """
    Categorizes a datasource's command, path, or template information.
    The categorization ignores first_of, head, and find since they depend on other
    datasources that will get resolved anyway. Ignore the listdir helper and explicit
    @datasource functions since they're pure python.
    """
    if isinstance(d, sf.simple_file):
        return ("file_static", [d.path])

    if isinstance(d, sf.first_file):
        return ("file_static", d.paths)

    if isinstance(d, sf.glob_file):
        return ("file_glob", d.patterns)

    if isinstance(d, sf.foreach_collect):
        return ("file_template", [d.path])

    if isinstance(d, sf.simple_command):
        return ("command_static", [d.cmd])

    if isinstance(d, sf.command_with_args):
        return ("command_template", [d.cmd])

    if isinstance(d, sf.foreach_execute):
        return ("command_template", [d.cmd])

    return (None, None)


def categorize(ds):
    """
    Extracts commands, paths, and templates from datasources and cateorizes them
    based on their type.
    """
    results = defaultdict(set)
    for d in ds:
        (cat, res) = resolve(d)
        if cat is not None:
            results[cat] |= set(res)
    return {k: sorted(v) for k, v in results.items()}


def get_spec_report():
    """
    You'll need to already have the specs loaded, and then you can call this
    procedure to get a categorized dict of the commands we might run and files
    we might collect.
    """
    load("insights.specs.default")
    ds = dr.get_components_of_type(datasource)
    return categorize(ds)


# helpers for running the script directly
# def parse_args():
#     p = argparse.ArgumentParser()
#     p.add_argument("-p", "--plugins", default=)
#     return p.parse_args()


def load(p):
    plugins = parse_plugins(p)
    load_packages(plugins)


# def main():
#     args = parse_args()
#     load(args.plugins)
#     report = get_spec_report()
#     print(yaml.dump(report))

def correct_format(parsed_data, expected_keys, filename):
    '''
    Ensure the parsed file matches the needed format
    Returns True, <message> on error
    Returns False, None on success
    '''
    # validate keys are what we expect
    def is_list_of_strings(data):
        '''
        Helper function for correct_format()
        '''
        if data is None:
            # nonetype, no data to parse. treat as empty list
            return True
        if not isinstance(data, list):
            return False
        for l in data:
            if not isinstance(l, six.string_types):
                return False
        return True

    keys = parsed_data.keys()
    invalid_keys = set(keys).difference(expected_keys)
    if invalid_keys:
        return True, ('Unknown section(s) in %s: ' % filename + ', '.join(invalid_keys) +
                      '\nValid sections are ' + ', '.join(expected_keys) + '.')

    # validate format (lists of strings)
    for k in expected_keys:
        if k in parsed_data:
            if k == 'patterns' and isinstance(parsed_data['patterns'], dict):
                if 'regex' not in parsed_data['patterns']:
                    return True, 'Patterns section contains an object but the "regex" key was not specified.'
                if 'regex' in parsed_data['patterns'] and len(parsed_data['patterns']) > 1:
                    return True, 'Unknown keys in the patterns section. Only "regex" is valid.'
                if not is_list_of_strings(parsed_data['patterns']['regex']):
                    return True, 'regex section under patterns must be a list of strings.'
                continue
            if not is_list_of_strings(parsed_data[k]):
                return True, '%s section must be a list of strings.' % k
    return False, None


def load_yaml(filename):
    try:
        with open(filename) as f:
            loaded_yaml = yaml.safe_load(f)
        if loaded_yaml is None:
            logger.debug('%s is empty.', filename)
            return {}
    except (yaml.YAMLError, yaml.parser.ParserError) as e:
        # can't parse yaml from conf
        raise RuntimeError('ERROR: Cannot parse %s.\n'
                           'If using any YAML tokens such as [] in an expression, '
                           'be sure to wrap the expression in quotation marks.\n\nError details:\n%s\n' % (filename, e))
    if not isinstance(loaded_yaml, dict):
        # loaded data should be a dict with at least one key
        raise RuntimeError('ERROR: Invalid YAML loaded.')
    return loaded_yaml


def verify_permissions(f):
    '''
    Verify 600 permissions on a file
    '''
    mode = stat.S_IMODE(os.stat(f).st_mode)
    if not mode == 0o600:
        raise RuntimeError("Invalid permissions on %s. "
                           "Expected 0600 got %s" % (f, oct(mode)))
    logger.debug("Correct file permissions on %s", f)


class InsightsUploadConf(object):
    """
    Insights spec configuration from uploader.json
    """

    def __init__(self, config, conn=None):
        """
        Load config from parent
        """
        self.config = config
        self.remove_file = config.remove_file
        self.redaction_file = config.redaction_file
        self.content_redaction_file = config.content_redaction_file

        # set rm_conf as a class attribute so we can observe it
        #   in create_report
        self.rm_conf = None

        # attribute to set when using file-redaction.conf instead of
        #   remove.conf, for reporting purposes. True by default
        #   since new format is favored.
        self.using_new_format = True

    def get_rm_conf_old(self):
        """
        Get excluded files config from remove_file.
        """
        # Convert config object into dict
        self.using_new_format = False
        parsedconfig = ConfigParser.RawConfigParser()
        if not os.path.isfile(self.remove_file):
            logger.debug('%s not found. No data files, commands,'
                         ' or patterns will be ignored, and no keyword obfuscation will occur.', self.remove_file)
            return None
        try:
            verify_permissions(self.remove_file)
        except RuntimeError as e:
            if self.config.validate:
                # exit if permissions invalid and using --validate
                raise RuntimeError('ERROR: %s' % e)
            logger.warning('WARNING: %s', e)
        try:
            parsedconfig.read(self.remove_file)
            sections = parsedconfig.sections()

            if not sections:
                # file has no sections, skip it
                logger.debug('Remove.conf exists but no parameters have been defined.')
                return None

            if sections != ['remove']:
                raise RuntimeError('ERROR: invalid section(s) in remove.conf. Only "remove" is valid.')

            rm_conf = {}
            for item, value in parsedconfig.items('remove'):
                if item not in expected_keys:
                    raise RuntimeError('ERROR: Unknown key in remove.conf: ' + item +
                                       '\nValid keys are ' + ', '.join(expected_keys) + '.')
                if six.PY3:
                    rm_conf[item] = value.strip().encode('utf-8').decode('unicode-escape').split(',')
                else:
                    rm_conf[item] = value.strip().decode('string-escape').split(',')
            self.rm_conf = rm_conf
        except ConfigParser.Error as e:
            # can't parse config file at all
            logger.debug(e)
            logger.debug('To configure using YAML, please use file-redaction.conf and file-content-redaction.conf.')
            raise RuntimeError('ERROR: Cannot parse the remove.conf file.\n'
                               'See %s for more information.' % self.config.logging_file)
        logger.warning('WARNING: remove.conf is deprecated. Please use file-redaction.conf and file-content-redaction.conf. See https://access.redhat.com/articles/4511681 for details.')
        return self.rm_conf

    def load_redaction_file(self, fname):
        '''
        Load the YAML-style file-redaction.conf
            or file-content-redaction.conf files
        '''
        if fname not in (self.redaction_file, self.content_redaction_file):
            # invalid function use, should never get here in a production situation
            return None
        if not os.path.isfile(fname):
            if fname == self.redaction_file:
                logger.debug('%s not found. No files or commands will be skipped.', self.redaction_file)
            elif fname == self.content_redaction_file:
                logger.debug('%s not found. '
                             'No patterns will be skipped and no keyword obfuscation will occur.', self.content_redaction_file)
            return None
        try:
            verify_permissions(fname)
        except RuntimeError as e:
            if self.config.validate:
                # exit if permissions invalid and using --validate
                raise RuntimeError('ERROR: %s' % e)
            logger.warning('WARNING: %s', e)
        loaded = load_yaml(fname)
        if fname == self.redaction_file:
            err, msg = correct_format(loaded, ('commands', 'files', 'components'), fname)
        elif fname == self.content_redaction_file:
            err, msg = correct_format(loaded, ('patterns', 'keywords'), fname)
        if err:
            # YAML is correct but doesn't match the format we need
            raise RuntimeError('ERROR: ' + msg)
        return loaded

    def get_rm_conf(self):
        '''
        Try to load the the "new" version of
        remove.conf (file-redaction.conf and file-redaction.conf)
        '''
        rm_conf = {}
        redact_conf = self.load_redaction_file(self.redaction_file)
        content_redact_conf = self.load_redaction_file(self.content_redaction_file)

        if redact_conf:
            rm_conf.update(redact_conf)
        if content_redact_conf:
            rm_conf.update(content_redact_conf)

        if not redact_conf and not content_redact_conf:
            # no file-redaction.conf or file-content-redaction.conf defined,
            #   try to use remove.conf
            return self.get_rm_conf_old()

        # remove Nones, empty strings, and empty lists
        filtered_rm_conf = dict((k, v) for k, v in rm_conf.items() if v)
        self.rm_conf = filtered_rm_conf
        return filtered_rm_conf

    def validate(self):
        '''
        Validate remove.conf
        '''
        success = self.get_rm_conf()
        if not success:
            logger.info('No contents in the blacklist configuration to validate.')
            return None
        # Using print here as this could contain sensitive information
        print('Blacklist configuration parsed contents:')
        print(success)
        logger.info('Parsed successfully.')
        return True

    def create_report(self):
        def length(lst):
            '''
            Because of how the INI remove.conf is parsed,
            an empty value in the conf will produce
            the value [''] when parsed. Do not include
            these in the report
            '''
            if len(lst) == 1 and lst[0] == '':
                return 0
            return len(lst)

        num_commands = 0
        num_files = 0
        num_patterns = 0
        num_keywords = 0
        num_components = 0
        using_regex = False
        using_new_format = False

        if self.rm_conf:
            for key in self.rm_conf:
                if key == 'commands':
                    num_commands = length(self.rm_conf['commands'])
                if key == 'files':
                    num_files = length(self.rm_conf['files'])
                if key == 'components':
                    num_components = length(self.rm_conf['components'])
                if key == 'patterns':
                    if isinstance(self.rm_conf['patterns'], dict):
                        num_patterns = length(self.rm_conf['patterns']['regex'])
                        using_regex = True
                    else:
                        num_patterns = length(self.rm_conf['patterns'])
                if key == 'keywords':
                    num_keywords = length(self.rm_conf['keywords'])

        output = {}
        output['obfuscate'] = self.config.obfuscate
        output['obfuscate_hostname'] = self.config.obfuscate_hostname
        output['commands'] = num_commands
        output['files'] = num_files
        output['components'] = num_components
        output['patterns'] = num_patterns
        output['keywords'] = num_keywords
        output['using_new_format'] = self.using_new_format
        output['using_patterns_regex'] = using_regex
        return output


if __name__ == '__main__':
    from .config import InsightsConfig
    config = InsightsConfig().load_all()
    uploadconf = InsightsUploadConf(config)
    uploadconf.validate()
    report = uploadconf.create_report()

    print(report)
