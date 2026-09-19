"""Service catalog — one normalized list of proxied services, built from definitions.

Consumers (SWAG site-confs, Homepage, docs generation) share this resolution
instead of each re-deriving upstreams from container/host/vm definitions.

Pure Python with no Ansible imports so homelab-docs' generate_docs.py can
import it directly. Values it does not understand (e.g. unrendered
``{{ vault_* }}`` strings when run without vault) are passed through as-is.

Each catalog entry:
    key     — definition key (container / host / vm name, ``_<n>`` suffix for list entries)
    kind    — 'label' | 'proxy_key' | 'host_def' | 'vm_def'
    role    — resolved host role (vm_definitions / host_definitions key), '' if unknown
    swag    — the SWAG site-conf dict consumed by templates/swag_site.conf.j2

Semantics mirror the Jinja this replaced in deploy_swag_configs.yaml: a key that
is present always wins over a default (Jinja ``default()`` only fires on
undefined), even when its value is empty.

Usage (Ansible):
    {{ container_definitions | swag_proxies(vm_definitions, host_definitions, _swag_role) }}
"""

from collections.abc import Mapping, Sequence

_LBL = 'homelab.proxy.'


def _first_port(definition):
    ports = (definition.get('compose') or {}).get('ports', [])
    return str(ports[0]).split(':')[0] if len(ports) > 0 else ''


def _stack_role(stack, vm_definitions, host_definitions):
    """First vm_definitions role whose stacks include ``stack``, else first host_definitions one."""
    for defs in (vm_definitions, host_definitions or {}):
        for role, rdef in defs.items():
            if 'stacks' in rdef and stack in rdef['stacks']:
                return role
    return ''


def _container_upstream(name, role, explicit, vm_definitions, swag_role):
    if explicit:
        return explicit
    if role == swag_role:
        # Same VM as SWAG — use the Docker name to avoid hairpin NAT
        return name
    if role and 'vm_ip' in (vm_definitions.get(role) or {}):
        return vm_definitions[role]['vm_ip']
    return ''


def _is_true(value):
    return str(value).lower() == 'true'


def _fill_location_ports(locations, port):
    return [loc if 'port' in loc else dict(loc, port=port) for loc in locations]


def _base_config(name, proxy, upstream_ip, port, source, default_subdomain=None):
    return {
        'name': name,
        'subdomain': proxy.get('subdomain', default_subdomain or name),
        'aliases': proxy.get('aliases', []),
        'upstream_ip': upstream_ip,
        'port': port,
        'proto': proxy.get('proto', 'http'),
        'auth': proxy.get('auth', True),
        'external': proxy.get('external', True),
        'error_pages': proxy.get('error_pages', False),
        'locations': proxy.get('locations', [{'path': '/'}]),
        'source': source,
    }


def _with_optional(config, proxy, keys):
    for key in keys:
        if key in proxy:
            config[key] = proxy[key]
    config['locations'] = _fill_location_ports(config['locations'], config['port'])
    return config


def _label_entries(container_definitions, vm_definitions, host_definitions, swag_role):
    """Simple containers: homelab.proxy.* labels, single '/' location."""
    for name, definition in container_definitions.items():
        labels = (definition.get('compose') or {}).get('labels', {})
        if _LBL + 'subdomain' not in labels or labels.get(_LBL + 'complex', 'false') == 'true':
            continue
        role = _stack_role(definition.get('stack', ''), vm_definitions, host_definitions)
        port = labels.get(_LBL + 'port', _first_port(definition))
        proto = labels.get(_LBL + 'proto', 'http')
        auth = _is_true(labels.get(_LBL + 'auth', 'true'))
        websocket = _is_true(labels.get(_LBL + 'websocket', 'false'))
        buffering = labels.get(_LBL + 'buffering', '')
        yield {
            'key': name,
            'kind': 'label',
            'role': role,
            'swag': {
                'name': name,
                'subdomain': labels[_LBL + 'subdomain'],
                'aliases': [a for a in labels.get(_LBL + 'aliases', '').split(',') if a],
                'upstream_ip': _container_upstream(
                    name, role, labels.get(_LBL + 'upstream_name', ''), vm_definitions, swag_role),
                'port': port,
                'proto': proto,
                'auth': auth,
                'external': _is_true(labels.get(_LBL + 'external', 'true')),
                'websocket': websocket,
                'buffering': buffering,
                'error_pages': _is_true(labels.get(_LBL + 'error_pages', 'false')),
                'locations': [{
                    'path': '/',
                    'port': port,
                    'proto': proto,
                    'auth': auth,
                    'websocket': websocket,
                    'buffering': buffering,
                }],
                'source': 'label',
            },
        }


def _proxy_key_entries(container_definitions, vm_definitions, host_definitions, swag_role):
    """Complex containers: full proxy: dict on the container definition."""
    for name, definition in container_definitions.items():
        if 'proxy' not in definition:
            continue
        proxy = definition['proxy']
        role = _stack_role(definition.get('stack', ''), vm_definitions, host_definitions)
        # upstream_role overrides the auto-detected role (multi-VM stacks with profiles)
        if proxy.get('upstream_role'):
            role = proxy['upstream_role']
        upstream = proxy.get('upstream_ip') or proxy.get('upstream_name') or ''
        config = _base_config(
            name, proxy,
            _container_upstream(name, role, upstream, vm_definitions, swag_role),
            _first_port(definition), 'proxy_key')
        yield {
            'key': name,
            'kind': 'proxy_key',
            'role': role,
            'swag': _with_optional(config, proxy, ('server_names', 'server_directives', 'buffer_size')),
        }


def _as_list(proxy):
    """(suffix, proxy) pairs — a mapping keeps the bare name, a list gets _<index>."""
    if isinstance(proxy, Mapping):
        return [('', proxy)]
    if isinstance(proxy, Sequence) and not isinstance(proxy, str):
        return [('_%d' % i, p) for i, p in enumerate(proxy)]
    return []


def _host_entries(host_definitions):
    """Non-VM hosts (unRAID, NAS, appliances): proxy: dict or list on host_definitions."""
    for name, definition in (host_definitions or {}).items():
        for suffix, proxy in _as_list(definition.get('proxy')):
            config = _base_config(
                name + suffix, proxy, proxy.get('upstream_ip', ''), proxy.get('port', ''), 'host_def', name)
            yield {
                'key': name + suffix,
                'kind': 'host_def',
                'role': name,
                'swag': _with_optional(config, proxy, ('server_names', 'server_directives', 'buffer_size')),
            }


def _vm_entries(vm_definitions):
    """Proxmox VMs: proxy: dict or list on vm_definitions, upstream defaults to vm_ip."""
    for name, definition in vm_definitions.items():
        for suffix, proxy in _as_list(definition.get('proxy')):
            config = _base_config(
                name + suffix, proxy, proxy.get('upstream_ip', definition.get('vm_ip', '')),
                proxy.get('port', ''), 'vm_def', name)
            yield {
                'key': name + suffix,
                'kind': 'vm_def',
                'role': name,
                'swag': _with_optional(config, proxy, ('server_names', 'server_directives')),
            }


def service_catalog(container_definitions, vm_definitions, host_definitions=None, swag_role=''):
    """All proxied services, in SWAG generation order: label, proxy_key, host, vm."""
    return (list(_label_entries(container_definitions, vm_definitions, host_definitions, swag_role))
            + list(_proxy_key_entries(container_definitions, vm_definitions, host_definitions, swag_role))
            + list(_host_entries(host_definitions))
            + list(_vm_entries(vm_definitions)))


def swag_proxies(container_definitions, vm_definitions, host_definitions=None, swag_role=''):
    """The SWAG site-conf dicts from the catalog (input to templates/swag_site.conf.j2)."""
    return [e['swag'] for e in service_catalog(container_definitions, vm_definitions, host_definitions, swag_role)]


class FilterModule(object):
    def filters(self):
        return {
            'service_catalog': service_catalog,
            'swag_proxies': swag_proxies,
        }
