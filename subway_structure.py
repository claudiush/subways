import csv
import logging
import math
import urllib.parse
import urllib.request


SPREADSHEET_ID = '1-UHDzfBwHdeyFxgC5cE_MaNQotF3-Y0r1nW9IwpIEj8'
MAX_DISTANCE_NEARBY = 150  # in meters


def el_id(el):
    if 'type' not in el:
        raise Exception('What is this element? {}'.format(el))
    return el['type'][0] + str(el.get('id', el.get('ref', '')))


def el_center(el):
    if 'lat' in el:
        return (el['lon'], el['lat'])
    elif 'center' in el:
        if el['center']['lat'] == 0.0:
            # Some relations don't have centers. We need route_masters and stop_area_groups.
            if el['type'] == 'relation' and 'tags' in el and (
                    el['tags'].get('type', None) == 'route_master' or
                    el['tags'].get('public_transport', None) == 'stop_area_group'):
                return None
        return (el['center']['lon'], el['center']['lat'])
    return None


def distance(p1, p2):
    if p1 is None or p2 is None:
        return None
    dx = math.radians(p1[0] - p2[0]) * math.cos(
        0.5 * math.radians(p1[1] + p2[1]))
    dy = math.radians(p1[1] - p2[1])
    return 6378137 * math.sqrt(dx*dx + dy*dy)


class Station:
    @staticmethod
    def is_station(el):
        if el.get('tags', {}).get('railway', None) != 'station':
            return False
        if 'construction' in el['tags'] or 'proposed' in el['tags']:
            return False
        if (el['tags'].get('station', None) not in ('subway', 'light_rail') and
                el['tags'].get('subway', None) != 'yes' and
                el['tags'].get('light_rail', None) != 'yes'):
            return False
        return True

    def __init__(self, el, city):
        """Call this with a railway=station node."""
        if el.get('tags', {}).get('railway', None) != 'station':
            raise Exception(
                'Station object should be instantiated from a station node. Got: {}'.format(el))
        if not Station.is_station(el):
            raise Exception('Processing only subway and light rail stations')

        if el['type'] != 'node':
            city.warn('Station is not a node', el)
        self.element = el
        self.is_light = (el['tags'].get('station', None) == 'light_rail' or
                         el['tags'].get('light_rail', None) == 'yes')
        self.id = el_id(el)
        self.elements = set([self.id])
        if self.id in city.stations:
            city.error('Station {} {} is listed in two stop_areas, first one:'.format(
                    el['type'], el['id']), city.stations[self.id].element)

        # Find a stop_area relation
        self.stop_area = None
        nearby = []
        center = el_center(el)
        if center is None:
            raise Exception('Could not find center of {}'.format(el))
        for d in city.elements.values():
            if 'tags' not in d:
                continue
            # If it's a stop_area relation containing "el", record it
            if d['type'] == 'relation' and d['tags'].get('public_transport', None) == 'stop_area':
                for m in d['members']:
                    if m['type'] == el['type'] and m['ref'] == el['id']:
                        self.stop_area = d
                        break
            # Otherwise record all platforms, stops and entrances nearby
            elif d['type'] != 'relation' and (
                    d['tags'].get('railway', None) in ('platform', 'subway_entrance') or
                    d['tags'].get('public_transport', None) in ('platform', 'stop_position')):
                # Take care to not add other stations
                if 'station' not in d['tags']:
                    d_center = el_center(d)
                    if d_center is not None and distance(center, d_center) <= MAX_DISTANCE_NEARBY:
                        nearby.append(d)
            if self.stop_area:
                break

        if self.stop_area:
            # If we have a stop area, add all elements from it
            self.elements.add(el_id(self.stop_area))
            for m in self.stop_area['members']:
                k = el_id(m)
                if k in city.elements:
                    self.elements.add(k)
        else:
            # Otherwise add nearby entrances and stop positions
            for k in nearby:
                self.elements.add(el_id(k))

        # TODO: Set name, colour etc.
        self.name = el['tags'].get('name', 'Unknown')
        self.colour = el['tags'].get('colour', None)

    def contains(self, el):
        return el_id(el) in self.elements


class Route:
    """The longest route for a city with a unique ref."""
    @staticmethod
    def is_route(el):
        if el['type'] != 'relation' or el.get('tags', {}).get('type', None) != 'route':
            return False
        if 'members' not in el:
            return False
        if el['tags'].get('route', None) not in ('subway', 'light_rail'):
            return False
        if 'construction' in el['tags'] or 'proposed' in el['tags']:
            return False
        if 'ref' not in el['tags'] and 'name' not in el['tags']:
            return False
        return True

    @staticmethod
    def get_network(relation):
        return relation['tags'].get('network', relation['tags'].get('operator', None))

    def __init__(self, relation, city):
        if not Route.is_route(relation):
            raise Exception('The relation does not seem a route: {}'.format(relation))
        self.element = relation
        self.id = el_id(relation)
        if 'ref' not in relation['tags']:
            city.warn('Missing ref on a route', relation)
        self.ref = relation['tags'].get('ref', relation['tags'].get('name', None))
        if 'colour' not in relation['tags']:
            city.warn('Missing colour on a route', relation)
        self.colour = relation['tags'].get('colour', None)
        self.network = Route.get_network(relation)
        self.is_light = relation['tags']['route'] == 'light_rail'
        self.rails = []
        self.stops = []
        enough_stops = False
        for m in relation['members']:
            k = el_id(m)
            if k in city.stations:
                st = city.stations[k]
                if not self.stops or self.stops[-1] != st:
                    if enough_stops:
                        if st not in self.stops:
                            city.warn('Inconsistent platform-stop "{}" in route'.format(st.name),
                                      relation)
                    elif st not in self.stops:
                        self.stops.append(st)
                        if self.is_light and not st.is_light:
                            city.warn('Subway station "{}" in light rail route'.format(st.name),
                                      relation)
                        elif st.is_light and not self.is_light:
                            city.warn('Light rail station "{}" in subway route'.format(st.name),
                                      relation)
                    elif self.stops[0] == st and not enough_stops:
                        enough_stops = True
                    else:
                        city.warn('Duplicate stop "{}" in route - check stop/platform order'.format(
                            st.name), relation)
                continue

            if k not in city.elements:
                if m['role'] in ('stop', 'platform'):
                    city.error('{} {} {} for route relation is not in the dataset'.format(
                        m['role'], m['type'], m['ref']), relation)
                    raise Exception('Stop or platform is not in the dataset')
                continue
            el = city.elements[k]
            if 'tags' not in el:
                city.error('Untagged object in a route', relation)
                continue
            if m['role'] in ('stop', 'platform'):
                if el['tags'].get('railway', None) in ('station', 'halt'):
                    city.error('Missing station={} on a {}'.format(
                        'light_rail' if self.is_light else 'subway', m['role']), el)
                elif 'construction' in el['tags'] or 'proposed' in el['tags']:
                    city.error('An under construction {} in route'.format(m['role']), el)
                else:
                    city.error('{} {} {} is not connected to a station in route'.format(
                        m['role'], m['type'], m['ref']), relation)
            if el['tags'].get('railway', None) in ('rail', 'subway', 'light_rail'):
                if 'nodes' in el:
                    self.rails.append((el['nodes'][0], el['nodes'][1]))
                else:
                    city.error('Cannot find nodes in a railway', el)
                continue
        if not self.stops:
            city.error('Route has no stops', relation)
        for i in range(1, len(self.rails)):
            connected = sum([(1 if self.rails[i][j[0]] == self.rails[i-1][j[1]] else 0)
                             for j in ((0, 0), (0, 1), (1, 0), (1, 1))])
            if not connected:
                city.warn('Hole in route rails near node {}'.format(self.rails[i][0]), relation)
                break


class RouteMaster:
    def __init__(self, route):
        self.routes = [route]
        self.best = route
        self.ref = route.ref
        self.network = route.network
        self.is_light = route.is_light

    def add(self, route, city):
        if route.network != self.network:
            city.error('Route has different network ("{}") from master "{}"'.format(
                route.network, self.network), route.element)
        if route.ref != self.ref:
            city.warn('Route "{}" has different ref from master "{}"'.format(
                route.ref, self.ref), route.element)
        if route.is_light != self.is_light:
            city.error('Incompatible is_light flag: master has {} and route has {}'.format(
                self.is_light, route.is_light), route.element)
            return
        self.routes.append(route)
        if len(route.stops) > len(self.best.stops):
            self.best = route

    def __len__(self):
        return len(self.routes)

    def __get__(self, i):
        return self.routes[i]


class City:
    def __init__(self, row):
        self.name = row[0]
        self.country = row[1]
        self.continent = row[2]
        self.num_stations = int(row[3])
        self.num_lines = int(row[4] or '0')
        self.num_light_lines = int(row[5] or '0')
        self.num_interchanges = int(row[6] or '0')
        self.networks = set(filter(None, [x.strip() for x in row[8].split(';')]))
        bbox = row[7].split(',')
        if len(bbox) == 4:
            self.bbox = [float(bbox[i]) for i in (1, 0, 3, 2)]
        else:
            self.bbox = None
        self.elements = {}   # Dict el_id → el
        self.stations = {}   # Dict el_id → station
        self.routes = {}     # Dict route_ref → route
        self.masters = {}    # Dict el_id of route → el_id of route_master
        self.transfers = []  # List of lists of stations
        self.station_ids = set()  # Set of stations' el_id
        self.errors = 0
        self.warnings = 0

    def contains(self, el):
        center = el_center(el)
        if center:
            return (self.bbox[0] <= center[1] <= self.bbox[2] and
                    self.bbox[1] <= center[0] <= self.bbox[3])
        # Default is True, so we put elements w/o coords in all cities
        return True

    def add(self, el):
        if el['type'] == 'relation' and 'members' not in el:
            return
        self.elements[el_id(el)] = el
        if el['type'] == 'relation' and el.get('tags', {}).get('type', None) == 'route_master':
            for m in el['members']:
                if m['type'] == 'relation':
                    self.masters[el_id(m)] = el_id(el)

    def log(self, level, message, el):
        msg = '{}: {}'.format(self.name, message)
        if el:
            tags = el.get('tags', {})
            msg += ' ({} {}, "{}")'.format(
                el['type'], el['id'],
                tags.get('name', tags.get('ref', '')))
        logging.log(level, msg)

    def warn(self, message, el=None):
        self.warnings += 1
        self.log(logging.WARNING, message, el)

    def error(self, message, el=None):
        self.errors += 1
        self.log(logging.ERROR, message, el)

    def make_transfer(self, sag):
        transfer = set()
        for m in sag['members']:
            k = el_id(m)
            if k not in self.stations:
                return
            transfer.add(self.stations[k])
        if transfer:
            self.transfers.append(transfer)

    def extract_routes(self):
        for el in self.elements.values():
            if Station.is_station(el):
                station = Station(el, self)
                self.station_ids.add(station.id)
                for e in station.elements:
                    self.stations[e] = station

        for el in self.elements.values():
            if Route.is_route(el):
                if self.networks and Route.get_network(el) not in self.networks:
                    continue
                route = Route(el, self)
                k = self.masters.get(route.id, route.ref)
                if k not in self.routes:
                    self.routes[k] = RouteMaster(route)
                else:
                    self.routes[k].add(route, self)

            if (el['type'] == 'relation' and
                    el.get('tags', {}).get('public_transport', None) == 'stop_area_group'):
                self.make_transfer(el)

    def count_unused_entrances(self):
        stop_areas = set()
        for el in self.elements.values():
            if (el['type'] == 'relation' and 'tags' in el and
                    el['tags'].get('public_transport', None) == 'stop_area' and
                    'members' in el):
                stop_areas.update([el_id(m) for m in el['members']])
        unused = []
        not_in_sa = []
        for el in self.elements.values():
            if (el['type'] == 'node' and 'tags' in el and
                    el['tags'].get('railway', None) == 'subway_entrance'):
                i = el_id(el)
                if i not in self.stations:
                    unused.append(i)
                if i not in stop_areas:
                    not_in_sa.append(i)
        if unused:
            list_unused = '' if len(unused) > 20 else ': ' + ', '.join(unused)
            self.error('Found {} unused subway entrances{}'.format(len(unused), list_unused))
        if not_in_sa:
            self.warn('{} subway entrances are not in stop_area relations'.format(len(not_in_sa)))

    def validate(self):
        unused_stations = set(self.station_ids)
        for rmaster in self.routes.values():
            for st in rmaster.best.stops:
                unused_stations.discard(st.id)
        if unused_stations:
            self.warn('{} unused stations: {}'.format(
                len(unused_stations), ', '.join(unused_stations)))
        self.count_unused_entrances()
        light_rails = len([x for x in self.routes.values() if x.is_light])
        if len(self.routes) - light_rails != self.num_lines:
            self.error('Found {} subway lines, expected {}'.format(
                len(self.routes) - light_rails, self.num_lines))
        if light_rails != self.num_light_lines:
            self.error('Found {} light rail lines, expected {}'.format(
                light_rails, self.num_light_lines))
        station_count = len(self.station_ids) - len(unused_stations)
        if station_count != self.num_stations:
            self.error('Found {} stations in routes, expected {}'.format(
                station_count, self.num_stations))
        if len(self.transfers) != self.num_interchanges:
            self.error('Found {} interchanges, expected {}'.format(
                len(self.transfers), self.num_interchanges))

    def for_mapsme(self):
        stops = []
        transfers = []
        routes = []
        network = {'network': 'TODO', 'agency_id': 12345, 'routes': routes}
        result = {'stops': stops, 'transfers': transfers, 'networks': [network]}
        return result


def download_cities():
    url = 'https://docs.google.com/spreadsheets/d/{}/export?format=csv'.format(SPREADSHEET_ID)
    response = urllib.request.urlopen(url)
    if response.getcode() != 200:
        raise Exception('Failed to download cities spreadsheet: HTTP {}'.format(response.getcode()))
    data = response.read().decode('utf-8')
    r = csv.reader(data.splitlines())
    next(r)  # skipping the header
    names = set()
    cities = []
    for row in r:
        if len(row) > 7 and row[7]:
            cities.append(City(row))
            if row[0].strip() in names:
                logging.warning('Duplicate city name in the google spreadsheet: %s', row[0])
            names.add(row[0].strip())
    return cities