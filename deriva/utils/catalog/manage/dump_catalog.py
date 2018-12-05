from __future__ import print_function

import argparse
import importlib
import os
import re
import sys

from yapf.yapflib.yapf_api import FormatCode

from attrdict import AttrDict
from deriva.core import ErmrestCatalog, get_credential

from deriva.core.ermrest_config import tag as chaise_tags
from .deriva_file_templates import table_file_template, schema_file_template, catalog_file_template

IS_PY2 = (sys.version_info[0] == 2)
IS_PY3 = (sys.version_info[0] == 3)

if IS_PY3:
    from urllib.parse import urlparse, urlsplit, urlunsplit
else:
    from urlparse import urlparse, urlsplit, urlunsplit

yapf_style = {
    'based_on_style': 'pep8',
    'allow_split_before_dict_value': False,
    'split_before_first_argument': False,
    'disable_ending_comma_heuristic': True,
    'DEDENT_CLOSING_BRACKETS': True,
    'column_limit': 100
}


class DerivaConfig:
    # Load a configuration file and look for a groups dictionary.
    groups = {}
    tags = chaise_tags
    variables = {}

    def __init__(self, configfile):
        if configfile:
            configmod = load_module_from_path(configfile)
            DerivaConfig.groups = AttrDict(configmod.groups)


def load_module_from_path(file):
    """
    Load configuration file from a path.
    :param file:
    :return:
    """

    class AddPath():
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            sys.path.insert(0, self.path)

        def __exit__(self, exc_type, exc_value, traceback):
            try:
                sys.path.remove(self.path)
            except ValueError:
                pass

    dir, file = os.path.split(os.path.abspath(file))
    modname = os.path.splitext(file)[0]
    importlib.invalidate_caches()

    with AddPath(dir):
        try:
            mod = importlib.reload(sys.modules[modname])
        except KeyError:
            mod = importlib.import_module(modname)
    return mod


class DerivaCatalogToString:
    def __init__(self, catalog, provide_system_columns=True, variables=None):
        self._catalog = catalog
        self._model = catalog.getCatalogModel()
        self._provide_system_columns = provide_system_columns
        self._variables = variables
        if self._variables is None:
            self._variables = {}

    def substitute_variables(self, code):
        """
        Factor out code and replace with a variable name.
        :param code:
        :return:
        """
        for k, v in self._variables.items():
            varsub = r"(['\"])+{}\1".format(v)
            if k in DerivaConfig.tags:
                repl = 'chaise_tags.{}'.format(k)
            elif k in DerivaConfig.groups:
                repl = 'groups.{}'.format(k)
            else:
                repl = k
            code = re.sub(varsub, repl, code)
        return code

    def variable_to_str(self, name, value, substitute=True):
        """
        Print out a variable assignment on one line if empty, otherwise pretty print.
        :param name: Left hand side of assigment
        :param value: Right hand side of assignment
        :return:
        """

        s = '{} = {!r}\n'.format(name, value)
        if substitute:
            s = self.substitute_variables(s)
        return s

    def tag_variables_to_str(self, annotations):
        """
        For each convenient annotation name in tag_map, print out a variable declaration of the form annotation = v
        where v is the value of the annotation the dictionary.  If the tag is not in the set of annotations, do nothing.
        :param annotations:
        :return:
        """
        s = ''
        for t, v in chaise_tags.items():
            if v in annotations:
                s += self.variable_to_str(t, annotations[v])
                s += '\n'
        return s

    def annotations_to_str(self, annotations, var_name='annotations'):
        """
        Print out the annotation definition in annotations, substituting the python variable for each of the tags
        specified in tag_map.
        :param annotations:
        :param var_name:
        :return:
        """

        var_map = {v: k for k, v in self._variables.items()}
        if annotations == {}:
            s = '{} = {{}}\n'.format(var_name)
        else:
            s = '{} = {{'.format(var_name)
            for t, v in annotations.items():
                if t in var_map:
                    # Use variable value rather then inline annotation value.
                    s += self.substitute_variables('{!r}:{},'.format(t, var_map[t]))
                else:
                    s += "'{}' : {!r},".format(t, v)
            s += '}\n'
        return s

    def schema_to_str(self, schema_name):
        schema = self._model.schemas[schema_name]
        server = urlparse(self._catalog.get_server_uri()).hostname
        catalog_id = self._catalog.get_server_uri().split('/')[-1]

        s = schema_file_template.format(server=server, catalog_id=catalog_id, schema_name=schema_name,
                                        groups=self.variable_to_str('groups', DerivaConfig.groups, substitute=False),
                                        annotations=self.variable_to_str('annotations', schema.annotations),
                                        acls=self.variable_to_str('acls', schema.acls),
                                        comments=self.variable_to_str('comment', schema.comment),
                                        table_names='table_names = [\n{}]\n'.format(
                                            str.join('', ['{!r},\n'.format(i) for i in schema.tables])))
        s = FormatCode(s, style_config=yapf_style)[0]
        return s

    def catalog_to_str(self):
        server = urlparse(self._catalog.get_server_uri()).hostname
        catalog_id = self._catalog.get_server_uri().split('/')[-1]

        s = catalog_file_template.format(server=server, catalog_id=catalog_id,
                                         groups=self.variable_to_str('groups', DerivaConfig.groups, substitute=False),
                                         tag_variables=self.tag_variables_to_str(self._model.annotations),
                                         annotations=self.annotations_to_str(self._model.annotations),
                                         acls=self.variable_to_str('acls', self._model.acls))
        s = FormatCode(s, style_config=yapf_style)[0]
        return s

    def table_annotations_to_str(self, table):
        s = self.tag_variables_to_str(table.annotations)
        s += self.annotations_to_str(table.annotations, var_name='table_annotations')
        s += self.variable_to_str('table_comment', table.comment)
        s += self.variable_to_str('table_acls', table.acls)
        s += self.variable_to_str('table_acl_bindings', table.acl_bindings)
        return s

    def column_annotations_to_str(self, table):
        column_annotations = {}
        column_acls = {}
        column_acl_bindings = {}
        column_comment = {}

        for i in table.column_definitions:
            if not (i.annotations == '' or not i.comment):
                column_annotations[i.name] = i.annotations
            if not (i.comment == '' or not i.comment):
                column_comment[i.name] = i.comment
            if i.annotations != {}:
                column_annotations[i.name] = i.annotations
            if i.acls != {}:
                column_acls[i.name] = i.acls
            if i.acl_bindings != {}:
                column_acl_bindings[i.name] = i.acl_bindings
        s = self.variable_to_str('column_annotations', column_annotations) + '\n'
        s += self.variable_to_str('column_comment', column_comment) + '\n'
        s += self.variable_to_str('column_acls', column_acls) + '\n'
        s += self.variable_to_str('column_acl_bindings', column_acl_bindings) + '\n'
        return s

    def foreign_key_defs_to_str(self, table):
        s = 'fkey_defs = [\n'
        for fkey in table.foreign_keys:
            s += """    em.ForeignKey.define({},
                '{}', '{}', {},
                constraint_names={},\n""".format([c['column_name'] for c in fkey.foreign_key_columns],
                                                 fkey.referenced_columns[0]['schema_name'],
                                                 fkey.referenced_columns[0]['table_name'],
                                                 [c['column_name'] for c in fkey.referenced_columns],
                                                 fkey.names)

            for i in ['annotations', 'acls', 'acl_bindings', 'on_update', 'on_delete', 'comment']:
                a = getattr(fkey, i)
                if not (a == {} or a is None or a == 'NO ACTION' or a == ''):
                    v = "'" + a + "'" if re.match('comment|on_update|on_delete', i) else a
                    s += "        {}={},\n".format(i, v)
            s += '    ),\n'

        s += ']'
        s = self.substitute_variables(s)
        return s

    def key_defs_to_str(self, table):
        s = 'key_defs = [\n'
        for key in table.keys:
            s += """    em.Key.define({},
                       constraint_names={},\n""".format(key.unique_columns, key.names)
            for i in ['annotations',  'comment']:
                a = getattr(key, i)
                if not (a == {} or a is None or a == ''):
                    v = "'" + a + "'" if i == 'comment' else a
                    s += "       {} = {},\n".format(i, v)
            s += '),\n'
        s += ']'
        s = self.substitute_variables(s)
        return s

    def column_defs_to_str(self, table):
        system_columns = ['RID', 'RCB', 'RMB', 'RCT', 'RMT']

        s = 'column_defs = ['
        for col in table.column_definitions:
            if col.name in system_columns and self._provide_system_columns:
                continue
            s += '''    em.Column.define('{}', em.builtin_types['{}'],'''.\
                format(col.name, col.type.typename + '[]' if 'is_array' is True else col.type.typename)
            if col.nullok is False:
                s += "nullok=False,"
            if col.default and col.name not in system_columns:
                s += "default={!r},".format(col.default)
            for i in ['annotations', 'acls', 'acl_bindings', 'comment']:
                colvar = getattr(col, i)
                if colvar:  # if we have a value for this field....
                    s += "{}=column_{}['{}'],".format(i, i, col.name)
            s += '),\n'
        s += ']'
        return s

    def table_def_to_str(self):
        s = """table_def = em.Table.define(table_name,
        column_defs=column_defs,
        key_defs=key_defs,
        fkey_defs=fkey_defs,
        annotations=table_annotations,
        acls=table_acls,
        acl_bindings=table_acl_bindings,
        comment=table_comment,
        provide_system = {}
    )""".format(self._provide_system_columns)
        return s

    def table_to_str(self, schema_name, table_name):
        schema = self._model.schemas[schema_name]
        table = schema.tables[table_name]

        server = urlparse(self._catalog.get_server_uri()).hostname
        catalog_id = self._catalog.get_server_uri().split('/')[-1]

        s = table_file_template.format(server=server, catalog_id=catalog_id,
                                       table_name=table_name, schema_name=schema_name,
                                       groups=self.variable_to_str('groups', DerivaConfig.groups, substitute=False),
                                       column_annotations=self.column_annotations_to_str(table),
                                       column_defs=self.column_defs_to_str(table),
                                       table_annotations=self.table_annotations_to_str(table),
                                       key_defs=self.key_defs_to_str(table),
                                       fkey_defs=self.foreign_key_defs_to_str(table),
                                       table_def=self.table_def_to_str())
        s = FormatCode(s, style_config=yapf_style)[0]
        return s


def main():
    parser = argparse.ArgumentParser(description='Dump definition for catalog {}:{}')
    parser.add_argument('server', help='Catalog server name')
    parser.add_argument('--catalog', default=1, help='ID number of desired catalog')
    parser.add_argument('--dir', default="catalog-configs", help='output directory name)')
    parser.add_argument('--config', default=None, help='python script to set up configuration variables)')
    args = parser.parse_args()

    dumpdir = args.dir
    server = args.server
    catalog_id = args.catalog
    configfile = args.config

    try:
        os.makedirs(dumpdir, exist_ok=True)
    except OSError:
        print("Creation of the directory %s failed" % dumpdir)
        sys.exit(1)

    DerivaConfig(configfile)

    variables = {k: v for k, v in DerivaConfig.groups.items()}
    variables.update(DerivaConfig.tags)

    credential = get_credential(server)
    catalog = ErmrestCatalog('https', server, catalog_id, credentials=credential)
    model_root = catalog.getCatalogModel()

    print("Dumping catalog def....")
    stringer = DerivaCatalogToString(catalog, variables=variables)

    catalog_string = stringer.catalog_to_str()

    with open('{}/{}_{}.py'.format(dumpdir, server, catalog_id), 'w') as f:
        print(catalog_string, file=f)

    for schema_name in model_root.schemas:
        print("Dumping schema def for {}....".format(schema_name))
        schema_string = stringer.schema_to_str(schema_name)

        with open('{}/{}.schema.py'.format(dumpdir, schema_name), 'w') as f:
            print(schema_string, file=f)

    for schema_name, schema in model_root.schemas.items():
        for i in schema.tables:
            print('Dumping {}:{}'.format(schema_name, i))
            table_string = stringer.table_to_str(schema_name, i)
            filename = '{}/{}/{}.py'.format(dumpdir, schema_name, i)
            os.makedirs(os.path.dirname(filename), exist_ok=True)
            with open(filename, 'w') as f:
                print(table_string, file=f)


if __name__ == "__main__":
    main()
