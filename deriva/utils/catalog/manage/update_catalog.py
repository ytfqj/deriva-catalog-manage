import argparse
from requests.exceptions import HTTPError

from deriva.core import ErmrestCatalog, get_credential

def parse_args(server, catalog_id, is_table=False, is_catalog=False):
    parser = argparse.ArgumentParser(description='Update catalog configuration')
    parser.add_argument('--server', default=server, help='Catalog server name')
    parser.add_argument('--catalog_id', default=catalog_id, help='ID of desired catalog')
    parser.add_argument('--replace', action='store_true',
                        help='Replace existing values with new ones.  Otherwise, attempt to merge in values provided.')

    if is_table:
        modes = ['table', 'annotations', 'acls', 'comments', 'keys', 'fkeys', 'columns']
    elif is_catalog:
        modes = ['annotations', 'acls']
        parser.add_argument('--recurse', action='store_true',
                            help='Update all schema and tables in the catalog.')
    else:
        modes = ['schema', 'annotations', 'acls', 'comments']
        parser.add_argument('--recurse', action='store_true',
                            help='Update all tables in the schema.')

    parser.add_argument('mode', choices=modes,
                        help='Model element to be updated.')

    args = parser.parse_args()
    return args.mode, args.replace, args.server, args.catalog_id


class CatalogUpdaterException(Exception):
    def __init__(self, msg='Catalog Update Exception'):
        self.msg = msg


class CatalogUpdater:
    def __init__(self, catalog):
        self._catalog = catalog  # type: ErmrestCatalog

    def update_annotations(self, o, annotations, replace=False):
        if replace:
            o.annotations.clear()
        o.annotations.update(annotations)

    def update_acls(self, o, acls, replace=False):
        if replace:
            o.acls.clear()
        o.acls.update(acls)

    def update_acl_bindings(self, o, acl_bindings, replace=False):
        if replace:
            o.acls_binding.clear()
        o.acl_bindings.update(acl_bindings)

    def update_catalog(self, mode, annotations, acls, replace=False):
        if mode not in ['annotations', 'acls']:
            raise CatalogUpdaterException(msg="Unknown mode {}".format(mode))

        model = self._catalog.getCatalogModel()
        if mode == 'annotations':
            self.update_annotations(model, annotations, replace=replace)
        elif mode == 'acls':
            self.update_acls(model, acls, replace=replace)
        model.apply(self._catalog)

    def update_schema(self, mode, schema_def, replace=False):
        schema_name = schema_def['schema_name']
        annotations = schema_def['annotations']
        acls = schema_def['acls']
        comment = schema_def.get('comment', None)

        if mode not in ['schema', 'annotations', 'comment', 'acls']:
            raise CatalogUpdaterException(msg="Unknown mode {}".format(mode))

        model = self._catalog.getCatalogModel()
        if mode == 'schema':
            if replace:
                schema = model.schemas[schema_name]
                print('Deleting schema ', schema.name)
                ok = input('Type YES to confirm:')
                if ok == 'YES':
                    schema.delete(self._catalog, model)
            schema = model.create_schema(self._catalog, schema_def)
        else:
            schema = model.schemas[schema_name]
            if mode == 'annotations':
                self.update_annotations(schema, annotations, replace=replace)
            elif mode == 'acls':
                self.update_acls(schema, acls, replace=replace)
            elif mode == 'comment':
                schema.comment = comment
            schema.apply(self._catalog)

        return schema

    def update_table(self, mode, schema_name, table_def, replace=False):

        schema = self._catalog.getCatalogModel().schemas[schema_name]
        table_name = table_def['table_name']
        column_defs = table_def['column_definitions']
        table_acls = table_def['acls']
        table_acl_bindings = table_def['acl_bindings']
        table_annotations = table_def['annotations']
        table_comment = table_def.get('comment', None)
        key_defs = table_def['keys']
        fkey_defs = table_def['foreign_keys']

        print('Updating {}:{}'.format(schema_name, table_name))
        if mode not in ['table', 'columns', 'fkeys', 'keys', 'annotations', 'comment', 'acls']:
            raise CatalogUpdaterException(msg="Unknown mode {}".format(mode))

        skip_fkeys = False

        if mode == 'table':
            if replace:
                table = schema.tables[table_name]
                print('Deleting table ', table.name)
                ok = input('Type YES to confirm:')
                if ok == 'YES':
                    table.delete(self._catalog, schema)
                schema = self._catalog.getCatalogModel().schemas[schema_name]
            if skip_fkeys:
                table_def.fkey_defs = []
            print('Creating table...', table_name)
            table = schema.create_table(self._catalog, table_def)
            return table

        table = schema.tables[table_name]
        if mode == 'columns':
            if replace:
                table = schema.tables[table_name]
                print('Deleting columns ', table.name)
                ok = input('Type YES to confirm:')
                if ok == 'YES':
                    for k in table.column_definitions:
                        k.delete(self._catalog, table)
            # Go through the column definitions and add a new column if it doesn't already exist.
            for i in column_defs:
                try:
                    print('Creating column {}'.format(i['name']))
                    table.create_column(self._catalog, i)
                except HTTPError as e:
                    if 'already exists' in e.args:
                        print("Skipping existing column {}".format(i['names']))
                    else:
                        print("Skipping: column key {} {}: \n{}".format(i['names'], i, e.args))
        if mode == 'fkeys':
            if replace:
                print('deleting foreign_keys')
                for k in table.foreign_keys:
                    k.delete(self._catalog, table)
            for i in fkey_defs:
                try:
                    table.create_fkey(self._catalog, i)
                    print('Created foreign key {} {}'.format(i['names'], i))
                except HTTPError as e:
                    if 'already exists' in e.args:
                        print("Skipping existing foreign key {}".format(i['names']))
                    else:
                        print("Skipping: foreign key {} {}: \n{}".format(i['names'], i, e.args))

        if mode == 'keys':
            if replace:
                print('Deleting keys')
                for k in table.keys:
                    k.delete(self._catalog, table)
            for i in key_defs:
                try:
                    table.create_key(self._catalog, i)
                    print('Created key {}'.format(i['names']))
                except HTTPError as err:
                    if 'already exists' in err.response.text:
                        print("Skipping: key {} already exists".format(i['names']))
                    else:
                        print(err.response.text)
        if mode == 'annotations':
            self.update_annotations(table, table_annotations, replace=replace)

            column_annotations = {i['name']: i['annotations'] for i in column_defs}
            for c in table.column_definitions:
                if c.name in [column_annotations]:
                    self.update_annotations(c, column_annotations[c.name], replace=replace)

        if mode == 'comment':
            table.comment = table_comment
            column_comment = {i.name: i.annotations for i in column_defs}
            for c in table.column_definitions:
                if c.name in column_comment:
                    c.comment = column_comment[c.name]

        if mode == 'acls':
            self.update_acls(table, table_acls)
            self.update_acl_bindings(table, table_acl_bindings, replace=replace)

            column_acls = {i['name']: i['acls'] for i in column_defs if 'acls' in i}
            column_acl_bindings = {i['name']: i['acl_bindings'] for i in column_defs if 'acl_bindings in i'}
            for c in table.column_definitions:
                if c.name in column_acls:
                    self.update_acls(c, column_acls[c.name], replace=replace)
                if c.name in column_acl_bindings:
                    self.update_acl_bindings(c, column_acl_bindings[c.name], replace=replace)

        table.apply(self._catalog)
