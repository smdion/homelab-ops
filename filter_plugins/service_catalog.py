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
    definition / proxy — the raw container definition / proxy entry the entry came from

Semantics mirror the Jinja this replaced in deploy_swag_configs.yaml: a key that
is present always wins over a default (Jinja ``default()`` only fires on
undefined), even when its value is empty.

Usage (Ansible):
    {{ container_definitions | swag_proxies(vm_definitions, host_definitions, _swag_role) }}
"""

import re
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
            'definition': definition,
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
            'definition': definition,
            'proxy': proxy,
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
                'proxy': proxy,
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
                'proxy': proxy,
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


# ---------------------------------------------------------------------------
# Homepage dashboard
#
# A tile comes from a `dashboard:` dict on a container definition, on a host/vm
# proxy entry, or on one of that entry's locations (path-based services such as
# media.<domain>/sonarr). `hide: true` marks a service as deliberately absent.
# Pure externals (no IaC definition) come from vars/configs/homepage.yaml.
# ---------------------------------------------------------------------------

_TILE_FIELDS = ('icon', 'href', 'description', 'container')


def _role_addr(role, vm_definitions, vip_definitions):
    """Default address a widget uses to reach a role: its VM IP.

    NOT its keepalived VIP, even when the role has one — checked against every live
    widget (2026-09-19): only Authentik and CrowdSec actually use the core VIP: (.50);
    Tautulli, Seerr, Home Assistant and Grafana all sit on the same roles (core/apps)
    and use the plain VM IP. So VIP is opt-in per tile via an explicit
    ``widget.url: "http://{{ vault_core_vip }}:..."`` override, not a role default.
    ``vip_definitions`` is accepted for that per-tile use (e.g. Proxmox nodes via
    ``vault_pve_vip``) but is not consulted here.
    """
    return (vm_definitions.get(role) or {}).get('vm_ip', '')


def _published_port(definition, port):
    """Host port for ``port``: maps a container port through compose ports ("H:C"),
    and leaves a port that is already a published host port unchanged."""
    port = str(port)
    for mapping in (definition.get('compose') or {}).get('ports', []):
        parts = str(mapping).split('/')[0].split(':')
        if len(parts) >= 2 and parts[-1] == port:
            return parts[-2]
    return port


def _container_port(definition, port):
    """Container-side port for ``port``: maps a published host port through compose
    ports ("H:C"). Inverse of _published_port, for same-host container addressing."""
    port = str(port)
    for mapping in (definition.get('compose') or {}).get('ports', []):
        parts = str(mapping).split('/')[0].split(':')
        if len(parts) >= 2 and parts[-2] == port:
            return parts[-1]
    return port


def _container_name(key, definition):
    """The container_name compose gives this service (templates/compose.j2)."""
    return (definition.get('compose') or {}).get('container_name') or key.replace('_', '-')


def _local_target(key, definition, role, local_role, port):
    """(address, port) a widget should use for a container.

    When the container runs on the same host as Homepage, the host IP is the wrong
    answer: the homepage container reaching its own host's published port hairpins and
    is blocked (confirmed 2026-09-19 — every widget pointed at the apps VM's own IP
    failed from inside the container, both before and after the IaC cutover). They
    share the external 'homelab' docker network, so address it by container name on
    its internal port instead — the same rule deploy_swag_configs.yaml already uses
    for upstreams on SWAG's host. Returns (None, None) when the rule doesn't apply.
    """
    if not local_role or role != local_role:
        return None, None
    return _container_name(key, definition), _container_port(definition, port)


def _location_path(path):
    """URL base for a prefix location ('^~ /sonarr' -> '/sonarr'); '' for '/' or regex paths."""
    path = str(path or '/').strip()
    if path.startswith('^~ '):
        path = path[3:].strip()
    if not path.startswith('/') or any(c in path for c in ' ~*()?$='):
        return ''
    return path.rstrip('/')


def _secret_var(prefix, field):
    return 'HOMEPAGE_VAR_%s_%s' % (re.sub(r'[^A-Z0-9]+', '_', str(prefix).upper()).strip('_'), field.upper())


def _tile(key, dash, subdomain, addr, port, path, proto, domain, source):
    name = dash.get('name') or key.replace('_', ' ').title()
    widget = dict(dash['widget']) if dash.get('widget') else None
    secret_vars = {}
    if widget is not None:
        widget_proto = widget.pop('proto', proto)
        widget_port = widget.pop('port', port)
        if 'url' not in widget:
            widget['url'] = '%s://%s:%s%s' % (widget_proto, addr, widget_port, path)
        for field in widget.pop('secrets', []):
            secret_vars[field] = _secret_var(dash.get('secret_prefix') or name, field)
        # type and url first, for readable output
        widget = dict([(k, widget[k]) for k in ('type', 'url') if k in widget]
                      + [(k, v) for k, v in widget.items() if k not in ('type', 'url')])
    href = dash.get('href')
    if href is None:
        href = 'https://%s.%s%s' % (subdomain, domain, path) if subdomain else ''
    return {
        'key': key,
        'source': source,
        'group': dash.get('group', ''),
        'hide': bool(dash.get('hide')),
        'name': name,
        'icon': dash.get('icon', '%s.png' % (subdomain or key).lower()),
        'href': href,
        'description': dash.get('description', ''),
        'order': dash.get('order', 1000),
        'container': dash.get('container', ''),
        'widget': widget,
        'secret_vars': secret_vars,
    }


def _entry_tiles(entry, container_definitions, vm_definitions, vip_definitions, domain, local_role=''):
    swag = entry['swag']
    if entry['kind'] in ('label', 'proxy_key'):
        definition = entry['definition']
        dash = definition.get('dashboard')
        if not dash:
            return
        proxy = entry.get('proxy') or {}
        root = next((l for l in swag['locations'] if l.get('path', '/') == '/'), swag['locations'][0])
        raw_port = root.get('port', swag['port'])
        addr, port = (proxy.get('upstream_ip'), _published_port(definition, raw_port)) if proxy.get('upstream_ip') \
            else _local_target(entry['key'], definition, entry['role'], local_role, raw_port)
        if not addr:
            addr = _role_addr(entry['role'], vm_definitions, vip_definitions)
            port = _published_port(definition, raw_port)
        yield _tile(entry['key'], dash, swag['subdomain'], addr, port, '',
                    root.get('proto', swag['proto']), domain, entry['kind'])
        return
    proxy = entry['proxy']
    if proxy.get('dashboard'):
        root = next((l for l in swag['locations'] if l.get('path', '/') == '/'), swag['locations'][0])
        yield _tile(swag['subdomain'], proxy['dashboard'], swag['subdomain'],
                    root.get('upstream_app') or swag['upstream_ip'], root.get('port', swag['port']), '',
                    root.get('proto', swag['proto']), domain, entry['kind'])
    for loc in swag['locations']:
        dash = loc.get('dashboard')
        if not dash:
            continue
        upstream = loc.get('upstream_name') or loc.get('upstream_app') or swag['upstream_ip']
        port = loc.get('port', swag['port'])
        if upstream in container_definitions:
            # Docker name (reachable only from SWAG's host): widget goes via that container's host
            target = container_definitions[upstream]
            role = _stack_role(target.get('stack', ''), vm_definitions, None)
            upstream = _role_addr(role, vm_definitions, vip_definitions)
            port = _published_port(target, port)
        path = _location_path(loc.get('path'))
        yield _tile(path.strip('/') or swag['subdomain'], dash, swag['subdomain'], upstream, port, path,
                    loc.get('proto', swag['proto']), domain, entry['kind'])


# Path segments that mark a location as a helper/companion route (websocket channel,
# metrics scrape, unauthenticated API passthrough) rather than an independent,
# tile-worthy service — e.g. "^~ /sabnzbd/api" is not a service distinct from
# "^~ /sabnzbd", and "/wss" is not distinct from the "/" it accompanies.
_COMPANION_SEGMENTS = {'wss', 'ws', 'websocket', 'socket', 'metrics', 'api'}


def _named_units(locations):
    """Non-root locations on a multi-service subdomain (e.g. "media") that are each
    their own distinguishable service and so each need their own dashboard/hide —
    as opposed to a subdomain with a single "/" location, which needs one marker
    for the whole proxy entry. Excluded as companion routes riding along with
    whichever real unit they support (not distinct services of their own): a
    redirect (``redirect_only: true``), a known helper suffix (``/wss``, ``/api``,
    …), or — when the entry has a root "/" location — anything hitting that same
    port, which just means "another route into the same backend"."""
    root_port = next((l.get('port') for l in locations if str(l.get('path', '/')).strip() == '/'), None)
    units = []
    for loc in locations:
        if loc.get('redirect_only') or (root_port is not None and loc.get('port') == root_port):
            continue
        path = _location_path(loc.get('path'))
        if path and path.rsplit('/', 1)[-1] not in _COMPANION_SEGMENTS:
            units.append((path, loc))
    return units


def _is_covered(entry):
    """An entry — or, for a multi-service subdomain, every one of its named units —
    is placed on (or deliberately hidden from) the dashboard."""
    if entry['kind'] in ('label', 'proxy_key'):
        return bool(entry['definition'].get('dashboard'))
    proxy = entry['proxy']
    named = _named_units(proxy.get('locations') or [{'path': '/'}])
    if named:
        return all(loc.get('dashboard') for _, loc in named)
    return bool(proxy.get('dashboard'))


def dashboard_tiles(container_definitions, vm_definitions, host_definitions=None, vip_definitions=None,
                    domain='', swag_role='', externals=None, local_role='', amp_tiles=None):
    """Visible dashboard tiles (hidden ones dropped), from definitions + externals + AMP."""
    tiles = []
    catalog = service_catalog(container_definitions, vm_definitions, host_definitions, swag_role)
    in_catalog = set()
    for entry in catalog:
        if entry['kind'] in ('label', 'proxy_key'):
            in_catalog.add(entry['key'])
        tiles.extend(_entry_tiles(entry, container_definitions, vm_definitions, vip_definitions, domain, local_role))
    # Containers with a UI but no proxy entry (the tile must give its own href)
    for name, definition in container_definitions.items():
        if name not in in_catalog and definition.get('dashboard'):
            role = _stack_role(definition.get('stack', ''), vm_definitions, host_definitions)
            addr, port = _local_target(name, definition, role, local_role, _first_port(definition))
            tiles.append(_tile(name, definition['dashboard'], '',
                               addr or _role_addr(role, vm_definitions, vip_definitions),
                               port or _first_port(definition), '', 'http', domain, 'container'))
    for ext in externals or []:
        tiles.append(_tile(ext.get('key') or ext['name'].lower(), ext, '', '', '', '', 'http', domain, 'external'))
    tiles.extend(amp_tiles or [])
    # A container with both proxy labels and a proxy: key yields two entries — keep one tile
    seen, result = set(), []
    for t in tiles:
        if t['hide'] or (t['group'], t['name']) in seen:
            continue
        seen.add((t['group'], t['name']))
        result.append(t)   # a tile with no group surfaces in dashboard_problems
    return result


# ---------------------------------------------------------------------------
# AMP game servers
#
# AMP instances aren't declared in IaC — they're created in AMP's own UI, so the
# registry it keeps at ~amp/.ampdata/instances.json is the source of truth (the
# nightly backup reads the same file). The Games group is built from it, so a new
# game server appears on the dashboard without touching the repo.
# ---------------------------------------------------------------------------

def _amp_ports(instance):
    """[(name, port, protocol)] a GenericModule instance exposes, from DeploymentArgs."""
    import json as _json
    raw = (instance.get('DeploymentArgs') or {}).get('GenericModule.App.Ports')
    if not raw:
        return []
    try:
        ports = _json.loads(raw)
    except (ValueError, TypeError):
        return []
    return [(p.get('Name', ''), p.get('Port'), p.get('Protocol')) for p in ports if p.get('Port')]


def _amp_query_port(instance):
    """Port a gamedig query should hit: an explicit query port if the instance has one,
    else the module's primary application port."""
    ports = _amp_ports(instance)
    if not ports:
        return None
    for name, port, _ in ports:
        if 'query' in str(name).lower():
            return port
    args = instance.get('DeploymentArgs') or {}
    primary_ref = args.get('GenericModule.App.PrimaryApplicationPortRef')
    import json as _json
    try:
        for p in _json.loads(args.get('GenericModule.App.Ports') or '[]'):
            if p.get('Ref') == primary_ref:
                return p.get('Port')
    except (ValueError, TypeError):
        pass
    return ports[0][1]


def amp_game_tiles(instances, amp_addr, domain='', config=None):
    """Games-group tiles, one per AMP game instance (the ADS panel itself excluded).

    ``config`` (vars/configs/homepage.yaml homepage_amp): group, href, description,
    and ``widgets``: a game display name -> gamedig serverType map. A game with no
    mapping still gets a tile, just no widget — gamedig can't query every game.
    """
    cfg = config or {}
    widget_map = {str(k).lower(): v for k, v in (cfg.get('widgets') or {}).items()}
    tiles = []
    for inst in instances or []:
        if inst.get('Module') == 'ADS':
            continue                      # the AMP panel itself, not a game
        args = inst.get('DeploymentArgs') or {}
        game = args.get('GenericModule.App.DisplayName') or inst.get('ModuleDisplayName') or ''
        name = inst.get('FriendlyName') or inst.get('InstanceName')
        dash = {
            'group': cfg.get('group', 'Games'),
            'name': name,
            'description': cfg.get('description_prefix', '') + (game.lower() if game else 'game server'),
            'icon': cfg.get('icons', {}).get(game, '%s.png' % str(game or name).lower().replace(' ', '-')),
            'href': cfg.get('href', ''),
        }
        server_type = widget_map.get(str(game).lower())
        port = _amp_query_port(inst)
        if server_type and port:
            dash['widget'] = {'type': 'gamedig', 'serverType': server_type,
                              'url': 'udp://%s:%s' % (amp_addr, port)}
        tiles.append(_tile(str(name).lower(), dash, '', amp_addr, port or '', '', 'udp', domain, 'amp'))
    return tiles


def dashboard_uncovered(container_definitions, vm_definitions, host_definitions=None, swag_role=''):
    """Catalog entries — or, on a multi-service subdomain, the specific named units —
    with neither a dashboard group nor hide: true."""
    out = []
    for e in service_catalog(container_definitions, vm_definitions, host_definitions, swag_role):
        if e['kind'] in ('label', 'proxy_key'):
            if not e['definition'].get('dashboard'):
                out.append('%s (%s.*)' % (e['key'], e['swag']['subdomain']))
            continue
        proxy = e['proxy']
        named = _named_units(proxy.get('locations') or [{'path': '/'}])
        if named:
            out.extend('%s%s (%s.*%s)' % (e['key'], path, e['swag']['subdomain'], path)
                       for path, loc in named if not loc.get('dashboard'))
        elif not proxy.get('dashboard'):
            out.append('%s (%s.*)' % (e['key'], e['swag']['subdomain']))
    return out


def _layout_groups(layout):
    for tab in layout:
        for group in tab.get('groups', []):
            yield tab['tab'], group


def dashboard_problems(tiles, layout):
    """Tiles whose group is not in the registry (empty group included)."""
    known = {g['name'] for _, g in _layout_groups(layout)}
    return ['%s: unknown group %r' % (t['name'], t['group']) for t in tiles if t['group'] not in known]


def homepage_services(tiles, layout):
    """services.yaml structure, in registry order; empty groups omitted.

    Secret widget fields become {{HOMEPAGE_VAR_*}} placeholders here — call this inside the
    template, not in set_fact, so Ansible never sees them as expressions to template.
    """
    out = []
    for _, group in _layout_groups(layout):
        members = sorted((t for t in tiles if t['group'] == group['name']),
                         key=lambda t: (t['order'], t['name'].lower()))
        if not members:
            continue
        services = []
        for t in members:
            body = {f: t[f] for f in _TILE_FIELDS if t[f]}
            if t['widget'] is not None:
                widget = dict(t['widget'])
                for field, var in t['secret_vars'].items():
                    widget[field] = '{{%s}}' % var
                body['widget'] = widget
            services.append({t['name']: body})
        out.append({group['name']: services})
    return out


def homepage_layout(tiles, layout):
    """settings.yaml `layout:` mapping, in registry order; empty groups omitted."""
    used = {t['group'] for t in tiles}
    out = {}
    for tab, group in _layout_groups(layout):
        if group['name'] in used:
            out[group['name']] = {
                'tab': tab,
                'header': group.get('header', True),
                'style': group.get('style', 'row'),
                'columns': group.get('columns', 4),
            }
    return out


def homepage_secret_vars(tiles):
    """Every HOMEPAGE_VAR_* the generated services.yaml references."""
    return sorted({v for t in tiles for v in t['secret_vars'].values()})


# ---------------------------------------------------------------------------
# unRAID presence check
#
# unRAID containers are not in container_definitions (unRAID manages them), so nothing
# else notices when one is removed or a new UI appears. Each unRAID host lists the
# containers it is expected to run in host_definitions (unraid_containers); the daily
# run compares that list with `docker ps -a` and reports drift. It only ALERTS — the
# dashboard output depends on IaC alone, because unRAID's own update job restarts
# containers and a dashboard that followed live state would flicker.
# ---------------------------------------------------------------------------

def unraid_hosts(host_definitions):
    """[{name, fqdn, containers}] for every host that declares ``unraid_containers``."""
    return [{'name': name, 'fqdn': d.get('vm_hostname', ''), 'containers': list(d['unraid_containers'])}
            for name, d in (host_definitions or {}).items() if d.get('unraid_containers')]


def unraid_presence(results, hosts=None):
    """Drift findings from per-host `docker ps -a` results.

    ``results``: loop results, each with ``item`` = {name, containers} and either
    ``stdout_lines`` of ``<name>|<state>|<net.unraid.docker.webui label>`` or a failure.
    A container is *unlisted* only if it is running AND has a web UI label — the
    infrastructure containers without one (redis, exporters, ...) are not worth a warning.
    """
    findings = []
    for res in results or []:
        host = res.get('item') or {}
        name = host.get('name', '?')
        if res.get('unreachable') or res.get('failed') or res.get('rc', 0) != 0:
            reason = str(res.get('msg') or res.get('stderr') or 'no output').strip().splitlines()
            findings.append('%s: presence check could not run (%s)' % (name, reason[0][:90] if reason else 'unknown'))
            continue
        seen = {}
        for line in res.get('stdout_lines', []):
            parts = line.split('|', 2)
            if len(parts) == 3:
                seen[parts[0]] = (parts[1], parts[2])
        declared = set(host.get('containers') or [])
        for cname in sorted(declared):
            state = seen.get(cname, (None, ''))[0]
            if state is None:
                findings.append('%s: declared container %s is not present' % (name, cname))
            elif state != 'running':
                findings.append('%s: declared container %s is %s' % (name, cname, state))
        for cname, (state, webui) in sorted(seen.items()):
            if state == 'running' and webui and cname not in declared:
                findings.append('%s: unlisted container %s has a web UI — add it to unraid_containers '
                                'in host_definitions (and give it a dashboard tile)' % (name, cname))
    return findings


class FilterModule(object):
    def filters(self):
        return {
            'service_catalog': service_catalog,
            'amp_game_tiles': amp_game_tiles,
            'swag_proxies': swag_proxies,
            'dashboard_tiles': dashboard_tiles,
            'dashboard_uncovered': dashboard_uncovered,
            'dashboard_problems': dashboard_problems,
            'homepage_services': homepage_services,
            'homepage_layout': homepage_layout,
            'homepage_secret_vars': homepage_secret_vars,
            'unraid_presence': unraid_presence,
            'unraid_hosts': unraid_hosts,
        }
