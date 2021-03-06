#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from oslo_serialization import jsonutils
from requests import exceptions
import six

from heat.common import exception
from heat.common.i18n import _
from heat.common import template_format
from heat.common import urlfetch
from heat.engine import attributes
from heat.engine import environment
from heat.engine import properties
from heat.engine.resources import stack_resource
from heat.engine import template


def generate_class(name, template_name, env):
    data = TemplateResource.get_template_file(template_name, ('file',))
    tmpl = template.Template(template_format.parse(data))
    props, attrs = TemplateResource.get_schemas(tmpl, env.param_defaults)
    cls = type(name, (TemplateResource,),
               {'properties_schema': props,
                'attributes_schema': attrs})
    return cls


class TemplateResource(stack_resource.StackResource):
    '''
    A resource implemented by a nested stack.

    This implementation passes resource properties as parameters to the nested
    stack. Outputs of the nested stack are exposed as attributes of this
    resource.
    '''

    def __init__(self, name, json_snippet, stack):
        self._parsed_nested = None
        self.stack = stack
        self.validation_exception = None

        tri = self._get_resource_info(json_snippet)

        self.properties_schema = {}
        self.attributes_schema = {}

        # run Resource.__init__() so we can call self.nested()
        super(TemplateResource, self).__init__(name, json_snippet, stack)
        self.resource_info = tri
        if self.validation_exception is None:
            self._generate_schema(self.t)

    def _get_resource_info(self, rsrc_defn):
        tri = self.stack.env.get_resource_info(
            rsrc_defn.resource_type,
            resource_name=rsrc_defn.name,
            registry_type=environment.TemplateResourceInfo)
        if tri is None:
            self.validation_exception = ValueError(_(
                'Only Templates with an extension of .yaml or '
                '.template are supported'))
        else:
            self.template_name = tri.template_name
            self.resource_type = tri.name
            if tri.user_resource:
                self.allowed_schemes = ('http', 'https')
            else:
                self.allowed_schemes = ('http', 'https', 'file')

        return tri

    @staticmethod
    def get_template_file(template_name, allowed_schemes):
        try:
            return urlfetch.get(template_name, allowed_schemes=allowed_schemes)
        except (IOError, exceptions.RequestException) as r_exc:
            args = {'name': template_name, 'exc': six.text_type(r_exc)}
            msg = _('Could not fetch remote template '
                    '"%(name)s": %(exc)s') % args
            raise exception.NotFound(msg_fmt=msg)

    @staticmethod
    def get_schemas(tmpl, param_defaults):
        return ((properties.Properties.schema_from_params(
                tmpl.param_schemata(param_defaults))),
                (attributes.Attributes.schema_from_outputs(
                 tmpl[tmpl.OUTPUTS])))

    def _generate_schema(self, definition):
        self._parsed_nested = None
        try:
            tmpl = template.Template(self.child_template())
        except (exception.NotFound, ValueError) as download_error:
            self.validation_exception = download_error
            tmpl = template.Template(
                {"HeatTemplateFormatVersion": "2012-12-12"})

        # re-generate the properties and attributes from the template.
        self.properties_schema, self.attributes_schema = self.get_schemas(
            tmpl, self.stack.env.param_defaults)

        self.properties = definition.properties(self.properties_schema,
                                                self.context)
        self.attributes = attributes.Attributes(self.name,
                                                self.attributes_schema,
                                                self._resolve_attribute)

    def child_params(self):
        '''
        :return: parameter values for our nested stack based on our properties
        '''
        params = {}
        for pname, pval in iter(self.properties.props.items()):
            if not pval.implemented():
                continue

            try:
                val = self.properties[pname]
            except ValueError:
                if self.action == self.INIT:
                    prop = self.properties.props[pname]
                    val = prop.get_value(None)
                else:
                    raise

            if val is not None:
                # take a list and create a CommaDelimitedList
                if pval.type() == properties.Schema.LIST:
                    if len(val) == 0:
                        params[pname] = ''
                    elif isinstance(val[0], dict):
                        flattened = []
                        for (count, item) in enumerate(val):
                            for (ik, iv) in iter(item.items()):
                                mem_str = '.member.%d.%s=%s' % (count, ik, iv)
                                flattened.append(mem_str)
                        params[pname] = ','.join(flattened)
                    else:
                        # When None is returned from get_attr, creating a
                        # delimited list with it fails during validation.
                        # we should sanitize the None values to empty strings.
                        # FIXME(rabi) this needs a permanent solution
                        # to sanitize attributes and outputs in the future.
                        params[pname] = ','.join(
                            (x if x is not None else '') for x in val)
                else:
                    # for MAP, the JSON param takes either a collection or
                    # string, so just pass it on and let the param validate
                    # as appropriate
                    params[pname] = val

        return params

    def child_template(self):
        if not self._parsed_nested:
            self._parsed_nested = template_format.parse(self.template_data())
        return self._parsed_nested

    def regenerate_info_schema(self, definition):
        self._get_resource_info(definition)
        self._generate_schema(definition)

    def implementation_signature(self):
        self._generate_schema(self.t)
        return super(TemplateResource, self).implementation_signature()

    def template_data(self):
        # we want to have the latest possible template.
        # 1. look in files
        # 2. try download
        # 3. look in the db
        reported_excp = None
        t_data = self.stack.t.files.get(self.template_name)
        stored_t_data = t_data
        if not t_data and self.template_name.endswith((".yaml", ".template")):
            try:
                t_data = self.get_template_file(self.template_name,
                                                self.allowed_schemes)
            except exception.NotFound as err:
                if self.action == self.UPDATE:
                    raise
                reported_excp = err

        if t_data is None:
            if self.nested() is not None:
                t_data = jsonutils.dumps(self.nested().t.t)

        if t_data is not None:
            if t_data != stored_t_data:
                self.stack.t.files[self.template_name] = t_data
            self.stack.t.env.register_class(self.resource_type,
                                            self.template_name)
            return t_data
        if reported_excp is None:
            reported_excp = ValueError(_('Unknown error retrieving %s') %
                                       self.template_name)
        raise reported_excp

    def _validate_against_facade(self, facade_cls):
        facade_schemata = properties.schemata(facade_cls.properties_schema)

        for n, fs in facade_schemata.items():
            if fs.required and n not in self.properties_schema:
                msg = (_("Required property %(n)s for facade %(type)s "
                       "missing in provider") % {'n': n, 'type': self.type()})
                raise exception.StackValidationFailed(message=msg)

            ps = self.properties_schema.get(n)
            if (n in self.properties_schema and
                    (fs.allowed_param_prop_type() != ps.type)):
                # Type mismatch
                msg = (_("Property %(n)s type mismatch between facade %(type)s"
                       " (%(fs_type)s) and provider (%(ps_type)s)") % {
                           'n': n, 'type': self.type(),
                           'fs_type': fs.type, 'ps_type': ps.type})
                raise exception.StackValidationFailed(message=msg)

        for n, ps in self.properties_schema.items():
            if ps.required and n not in facade_schemata:
                # Required property for template not present in facade
                msg = (_("Provider requires property %(n)s "
                       "unknown in facade %(type)s") % {
                           'n': n, 'type': self.type()})
                raise exception.StackValidationFailed(message=msg)

        for attr in facade_cls.attributes_schema:
            if attr not in self.attributes_schema:
                msg = (_("Attribute %(attr)s for facade %(type)s "
                       "missing in provider") % {
                           'attr': attr, 'type': self.type()})
                raise exception.StackValidationFailed(message=msg)

    def validate(self):
        if self.validation_exception is not None:
            msg = six.text_type(self.validation_exception)
            raise exception.StackValidationFailed(message=msg)

        try:
            self.template_data()
        except ValueError as ex:
            msg = _("Failed to retrieve template data: %s") % ex
            raise exception.StackValidationFailed(message=msg)
        cri = self.stack.env.get_resource_info(
            self.type(),
            resource_name=self.name,
            registry_type=environment.ClassResourceInfo)

        # If we're using an existing resource type as a facade for this
        # template, check for compatibility between the interfaces.
        if cri is not None and not isinstance(self, cri.get_class()):
            facade_cls = cri.get_class()
            self._validate_against_facade(facade_cls)

        return super(TemplateResource, self).validate()

    def handle_adopt(self, resource_data=None):
        return self.create_with_template(self.child_template(),
                                         self.child_params(),
                                         adopt_data=resource_data)

    def handle_create(self):
        return self.create_with_template(self.child_template(),
                                         self.child_params())

    def metadata_update(self, new_metadata=None):
        '''
        Refresh the metadata if new_metadata is None
        '''
        if new_metadata is None:
            self.metadata_set(self.t.metadata())

    def handle_update(self, json_snippet, tmpl_diff, prop_diff):
        return self.update_with_template(self.child_template(),
                                         self.child_params())

    def handle_delete(self):
        return self.delete_nested()

    def FnGetRefId(self):
        if self.nested() is None:
            return six.text_type(self.name)

        if 'OS::stack_id' in self.nested().outputs:
            return self.nested().output('OS::stack_id')

        return self.nested().identifier().arn()

    def FnGetAtt(self, key, *path):
        stack = self.nested()
        if stack is None:
            return None

        def _get_inner_resource(resource_name):
            if self.nested() is not None:
                try:
                    return self.nested()[resource_name]
                except KeyError:
                    raise exception.ResourceNotFound(
                        resource_name=resource_name,
                        stack_name=self.nested().name)

        def get_rsrc_attr(resource_name, *attr_path):
            resource = _get_inner_resource(resource_name)
            return resource.FnGetAtt(*attr_path)

        def get_rsrc_id(resource_name):
            resource = _get_inner_resource(resource_name)
            return resource.FnGetRefId()

        # first look for explicit resource.x.y
        if key.startswith('resource.'):
            npath = key.split(".", 2)[1:] + list(path)
            if len(npath) > 1:
                return get_rsrc_attr(*npath)
            else:
                return get_rsrc_id(*npath)

        # then look for normal outputs
        if key in stack.outputs:
            return attributes.select_from_attribute(stack.output(key), path)

        # otherwise the key must be wrong.
        raise exception.InvalidTemplateAttribute(resource=self.name,
                                                 key=key)
