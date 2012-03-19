# -*- coding: utf-8 -*-
import os
from datetime import datetime, timedelta
from math import log, sqrt

from django.shortcuts import redirect, get_object_or_404
from django.core.urlresolvers import reverse
from django.db import connection
from django.http import Http404

from common.decorators import render_to
from common.pagination import paginate

import json, cPickle, base64

from us import statelist, statenames, stateapportionment

from person.models import Person, PersonRole
from person import analysis
from person.types import RoleType
from person.video import get_youtube_videos, get_sunlightlabs_videos
from person.util import get_committee_assignments

from events.models import Feed

from smartsearch.manager import SearchManager
from search import person_search_manager

from registration.helpers import json_response

@render_to('person/person_details.html')
def person_details(request, pk):
    person = get_object_or_404(Person, pk=pk)
    
    # redirect to canonical URL
    if request.path != person.get_absolute_url():
        return redirect(person.get_absolute_url(), permanent=True)
       
    # current role
    role = person.get_current_role()
    if role:
        active_role = True
    else:
        active_role = False
        try:
            role = person.roles.order_by('-enddate')[0]
        except IndexError:
            role = None

    # photo
    photo_path = 'data/photos/%d-100px.jpeg' % person.pk
    photo_credit = None
    if os.path.exists(photo_path):
        photo = '/' + photo_path
        with open(photo_path.replace("-100px.jpeg", "-credit.txt"), "r") as f:
            photo_credit = f.read().strip().split(" ", 1)
    else:
        photo = None

    analysis_data = analysis.load_data(person)

    return {'person': person,
            'role': role,
            'active_role': active_role,
            'photo': photo,
            'photo_credit': photo_credit,
            'analysis_data': analysis_data,
            'recent_bills': person.sponsored_bills.all().order_by('-introduced_date')[0:7],
            'committeeassignments': get_committee_assignments(person),
            'feed': Feed.PersonFeed(person.id),
            }


def searchmembers(request, initial_mode=None):
    return person_search_manager().view(request, "person/person_list.html",
        defaults = {
            "is_currently_serving": True if initial_mode=="current" else False,
            "text": request.GET["name"] if "name" in request.GET else None,
            },
        noun = ('person', 'people') )

def http_rest_json(url, args=None, method="GET"):
    import urllib, urllib2, json
    if method == "GET" and args != None:
        url += "?" + urllib.urlencode(args).encode("utf8")
    req = urllib2.Request(url)
    r = urllib2.urlopen(req)
    return json.load(r, "utf8")
    
@render_to('person/district_map.html')
def browsemembersbymap(request, state=None, district=None):
    center_lat, center_long, center_zoom = (38, -96, 4)
    
    sens = None
    reps = None
    if state != None:
        # Load senators for all states that are not territories.
        if stateapportionment[state] != "T":
            sens = Person.objects.filter(roles__current=True, roles__state=state, roles__role_type=RoleType.senator)
            sens = list(sens)
            for i in xrange(2-len(sens)): # make sure we list at least two slots
                sens.append(None)
    
        # Load representatives for at-large districts.
        reps = []
        if stateapportionment[state] in ("T", 1):
            dists = [0]
            if district != None:
                raise Http404()
                
        # Load representatives for non-at-large districts.
        else:
            dists = xrange(1, stateapportionment[state]+1)
            if district != None:
                if int(district) < 1 or int(district) > stateapportionment[state]:
                    raise Http404()
                dists = [int(district)]
        
        for i in dists:
            try:
                reps.append((i, Person.objects.get(roles__current=True, roles__state=state, roles__role_type=RoleType.representative, roles__district=i)))
            except Person.DoesNotExist:
                reps.append((i, None))

        if state == "MP":
            center_long, center_lat, center_zoom = (145.7, 15.1, 11.0)
        elif state == "AS":
            center_long, center_lat, center_zoom = (-170.255127, -14.514462, 8.0)
        else:
            cursor = connection.cursor()
            cursor.execute("SELECT MIN(X(PointN(ExteriorRing(bbox), 1))), MIN(Y(PointN(ExteriorRing(bbox), 1))), MAX(X(PointN(ExteriorRing(bbox), 3))), MAX(Y(PointN(ExteriorRing(bbox), 3))), SUM(Area(bbox)) FROM districtpolygons WHERE state=%s", [state])
            rows = cursor.fetchall()
            
            sw_lng, sw_lat, ne_lng, ne_lat, area = rows[0]
    
            center_long, center_lat = (sw_lng+ne_lng)/2.0, (sw_lat+ne_lat)/2.0
            center_zoom = round(1.0 - log(sqrt(area)/1000.0))
    
    return {
        "center_lat": center_lat,
        "center_long": center_long,
        "center_zoom": center_zoom,
        "state": state,
        "district": district,
        "stateapp": stateapportionment[state] if state != None else None,
        "statename": statenames[state] if state != None else None,
        "statelist": statelist,
        "senators": sens,
        "reps": reps,
    }

@render_to('person/district_map_embed.html')
def districtmapembed(request):
    return {
        "demo": "demo" in request.GET,
        "state": request.GET.get("state", ""),
        "district": request.GET.get("district", ""),
        "bounds": request.GET.get("bounds", None),
    }
    
@json_response
def district_lookup(request):
    lng, lat = float(request.GET.get("lng", "0")), float(request.GET.get("lat", "0"))
    return do_district_lookup(lng, lat)

def do_district_lookup(lng, lat):
    # Query based on bounding box.
    cursor = connection.cursor()
    cursor.execute("SELECT state, district, pointspickle FROM districtpolygons WHERE MBRContains(bbox, GeomFromText('Point(%s %s)'))", [lng, lat])
    rows = cursor.fetchall()
    
    # Do a point-in-polygon test for each polygon.
    for row in rows:
        poly = cPickle.loads(base64.b64decode(row[2]))
        if point_in_poly(lng, lat, poly):
            return { "state": row[0], "district": row[1] }

    return { "error": "point is not within a district" }

def point_in_poly(x, y, poly):
    # ray casting method
    n = len(poly)
    inside = False
    p1x, p1y = poly[0]
    for i in xrange(n+1):
        p2x, p2y = poly[i % n]
        if y > min(p1y, p2y):
            if y <= max(p1y, p2y):
                if x <= max(p1x, p2x):
                    if p1y != p2y:
                        xinters = (y-p1y)*(p2x-p1x)/(p2y-p1y)+p1x
                    if p1x == p2x or x <= xinters:
                        inside = not inside
        p1x, p1y = p2x, p2y
    return inside
    
@render_to('congress/political_spectrum.html')
def political_spectrum(request):
    rows = []
    fnames = [
        'data/us/112/stats/sponsorshipanalysis_h.txt',
        'data/us/112/stats/sponsorshipanalysis_s.txt',
    ]
    alldata = open(fnames[0]).read() + open(fnames[1]).read()
    for line in alldata.splitlines():
        chunks = [x.strip() for x in line.strip().split(',')]
        if chunks[0] == "ID":
            continue
        
        data = { }
        data['id'] = chunks[0]
        data['x'] = float(chunks[1])
        data['y'] = float(chunks[2])
        
        p = Person.objects.get(id=chunks[0])
        data['label'] = p.lastname
        if p.birthday: data['age'] = (datetime.now().date() - p.birthday).days / 365.25
        data['gender'] = p.gender
        
        r = p.get_last_role_at_congress(112)
        data['type'] = r.role_type
        data['party'] = r.party
        data['years_in_congress'] = (min(datetime.now().date(),r.enddate) - p.roles.filter(startdate__lte=r.startdate).order_by('startdate')[0].startdate).days / 365.25
        
        rows.append(data)
        
    years_in_congress_max = max([data['years_in_congress'] for data in rows])
    
    return {
        "data": json.dumps(rows),
        "years_in_congress_max": years_in_congress_max,
        }

