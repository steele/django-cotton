import functools
from typing import Union

from django.conf import settings
from django.template import Library
from django.template.base import (
    Node,
)
from django.template.context import Context
from django.template.loader import get_template

from django_cotton.utils import get_cotton_data
from django_cotton.exceptions import CottonIncompleteDynamicComponentError
from django_cotton.templatetags import Attrs, DynamicAttr, UnprocessableDynamicAttr

register = Library()


class CottonComponentNode(Node):
    def __init__(self, component_name, nodelist, attrs, only):
        self.component_name = component_name
        self.nodelist = nodelist
        self.attrs = attrs
        self.template_cache = {}
        self.only = only

    def render(self, context):
        cotton_data = get_cotton_data(context)

        # Push a new component onto the stack
        component_data = {
            "key": self.component_name,
            "attrs": Attrs({}),
            "slots": {},
        }
        cotton_data["stack"].append(component_data)

        # Process simple attributes and boolean attributes
        def process_attrs(attrs):
            for key, value in attrs.items():
                value = self._strip_quotes_safely(value)
                if value is True:  # Boolean attribute
                    component_data["attrs"][key] = True
                elif key.startswith(":"):
                    key = key[1:]
                    try:
                        if key == "attrs":  # Special case for spreading attributes
                            to_spread = DynamicAttr(value).resolve(context)
                            if not isinstance(to_spread, dict):
                                raise UnprocessableDynamicAttr()
                            process_attrs(to_spread)
                        else:
                            component_data["attrs"][key] = DynamicAttr(value).resolve(context)
                    except UnprocessableDynamicAttr:
                        component_data["attrs"].unprocessable(key)
                else:
                    component_data["attrs"][key] = value
        process_attrs(self.attrs)

        # Render the nodelist to process any slot tags and vars
        default_slot = self.nodelist.render(context)

        # Prepare the cotton-specific data
        component_state = {
            **component_data["slots"],
            **component_data["attrs"].make_attrs_accessible(),
            "attrs": component_data["attrs"],
            "slot": default_slot,
            "cotton_data": cotton_data,
        }

        template = self._get_cached_template(context, component_data["attrs"])

        # Isolate context if needed
        if self.only:
            output = template.render(Context(component_state))
        else:
            with context.push(component_state):
                output = template.render(context)

        cotton_data["stack"].pop()

        return output

    def _get_cached_template(self, context, attrs):
        cache = context.render_context.get(self)
        if cache is None:
            cache = context.render_context[self] = {}

        template_path = self._generate_component_template_path(self.component_name, attrs.get("is"))

        if template_path not in cache:
            template = get_template(template_path)
            if hasattr(template, "template"):
                template = template.template
            cache[template_path] = template

        return cache[template_path]

    @staticmethod
    @functools.lru_cache(maxsize=400)
    def _generate_component_template_path(component_name: str, is_: Union[str, None]) -> str:
        """Generate the path to the template for the given component name."""
        if component_name == "component":
            if is_ is None:
                raise CottonIncompleteDynamicComponentError(
                    'Cotton error: "<c-component>" should be accompanied by an "is" attribute.'
                )
            component_name = is_

        component_tpl_path = component_name.replace(".", "/")

        # Cotton by default will look for snake_case version of comp names. This can be configured to allow hyphenated names.
        snaked_cased_named = getattr(settings, "COTTON_SNAKE_CASED_NAMES", True)
        if snaked_cased_named:
            component_tpl_path = component_tpl_path.replace("-", "_")

        cotton_dir = getattr(settings, "COTTON_DIR", "cotton")
        return f"{cotton_dir}/{component_tpl_path}.html"

    @staticmethod
    def _strip_quotes_safely(value):
        if type(value) is str and value.startswith('"') and value.endswith('"'):
            return value[1:-1]
        return value


def cotton_component(parser, token):
    bits = token.split_contents()[1:]
    component_name = bits[0]
    attrs = {}
    only = False

    node_class = CottonComponentNode

    for bit in bits[1:]:
        if bit == "only":
            # if we see `only` we isolate context
            only = True
            continue
        try:
            key, value = bit.split("=")
            attrs[key] = value
        except ValueError:
            attrs[bit] = True

    nodelist = parser.parse(("endc",))
    parser.delete_first_token()

    return node_class(component_name, nodelist, attrs, only)
