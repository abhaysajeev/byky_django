/**
 * BYKY station map -- real tile basemap (Leaflet).
 *
 * Replaces the abstract SVG bubble plot that briefly stood in for this: that
 * drew station positions on a blank grid, which is a scatter chart, not a map.
 * Operators need the actual geography -- streets, coastline, satellite -- to
 * place a station, so the tile basemap is the point, not decoration.
 *
 * Drives every station map on the site. A page opts in with:
 *
 *   <div class="byky-map" data-map-points="<id of a json_script node>"></div>
 *
 * and loads leaflet's css/js plus this file. Marker radius tracks the fleet
 * held at each station.
 *
 * Basemaps (all keyless -- never Mapbox, which needs an access token):
 *   Light      Esri light-grey canvas + latin label layer. Default: OSM's own
 *              tiles render UAE place names in Arabic, and this UI is English.
 *              The neutral grey keeps the red station markers dominant.
 *   Streets    Esri World Street Map, when road context matters.
 *   Satellite  Esri World Imagery.
 *
 * Tile servers require a Referer; Django's default SECURE_REFERRER_POLICY of
 * "same-origin" strips it and every tile 403s, so settings.py sets
 * "strict-origin-when-cross-origin". Do not remove that.
 */

'use strict';

(function () {
  var ESRI = 'https://server.arcgisonline.com/ArcGIS/rest/services';
  var ESRI_ATTR = 'Tiles &copy; Esri &mdash; &copy; OpenStreetMap contributors';
  var RED = '#cc0000';

  function baseLayers() {
    var light = L.layerGroup([
      L.tileLayer(ESRI + '/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}', {
        attribution: ESRI_ATTR, maxZoom: 16
      }),
      L.tileLayer(ESRI + '/Canvas/World_Light_Gray_Reference/MapServer/tile/{z}/{y}/{x}', {
        maxZoom: 16
      })
    ]);
    var streets = L.tileLayer(ESRI + '/World_Street_Map/MapServer/tile/{z}/{y}/{x}', {
      attribution: ESRI_ATTR, maxZoom: 18
    });
    var satellite = L.layerGroup([
      L.tileLayer(ESRI + '/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
        attribution: ESRI_ATTR, maxZoom: 18
      }),
      L.tileLayer(ESRI + '/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}', {
        maxZoom: 18
      })
    ]);
    return { light: light, streets: streets, satellite: satellite };
  }

  function build(el) {
    var src = document.getElementById(el.dataset.mapPoints);
    if (!src) return;

    var points = JSON.parse(src.textContent) || [];
    if (!points.length) return;

    var layers = baseLayers();

    // Streets is the default: operators place stations against roads and
    // landmarks, and the grey canvas strips exactly that detail. Override per
    // screen with data-map-base="light|streets|satellite".
    var base = layers[el.dataset.mapBase] || layers.streets;

    var map = L.map(el, {
      scrollWheelZoom: false,
      layers: [base]
    }).setView([24.9, 55.4], 7);

    L.control.layers(
      { 'Streets': layers.streets, 'Light': layers.light, 'Satellite': layers.satellite },
      null,
      { position: 'topright' }
    ).addTo(map);

    var maxFleet = points.reduce(function (m, p) { return Math.max(m, p.fleet || 0); }, 0) || 1;

    var markers = points.map(function (p) {
      var radius = p.fleet ? 6 + Math.round((p.fleet / maxFleet) * 14) : 5;
      /* Both figures are indicative and dashboard-only, so each says so
         rather than reading as a measured count. */
      var onRent = p.on_rent
        ? '<div style="opacity:.75;margin-top:2px"><strong>' + p.on_rent + '</strong> on rent now</div>'
        : '';
      var revenue = p.revenue
        ? '<div style="opacity:.75;margin-top:4px">AED ' + Number(p.revenue).toLocaleString() + ' <span style="opacity:.7">indicative</span></div>'
        : '';
      return L.circleMarker([p.lat, p.lng], {
        radius: radius,
        color: RED,
        weight: 2,
        fillColor: RED,
        fillOpacity: p.fleet ? 0.45 : 0.12
      })
        .bindPopup(
          '<div style="min-width:170px">' +
            '<div style="font-weight:600;margin-bottom:2px">' + p.name + '</div>' +
            '<div style="opacity:.7;margin-bottom:6px">' + p.emirate + '</div>' +
            '<div><strong>' + p.fleet + '</strong> vehicles</div>' +
            onRent +
            revenue +
          '</div>'
        )
        .addTo(map);
    });

    // Open on the densest part of the network rather than the whole footprint.
    // Fitting all 36 stations pulls the view out to Bahrain/Qatar/Oman and the
    // markers collapse into unreadable blobs; the emirate the fleet actually
    // lives in is the useful first frame. Everything else stays plotted --
    // zooming out reveals Abu Dhabi, Al Ain, RAK, Fujairah and Kuwait.
    var focusEmirate = el.dataset.mapFocus || 'Dubai';
    var focus = markers.filter(function (m, i) { return points[i].emirate === focusEmirate; });

    // Fall back to the UAE cluster if that emirate holds no stations. Kuwait is
    // excluded from any auto-fit -- one station would otherwise frame the Gulf.
    if (!focus.length) {
      focus = markers.filter(function (m, i) { return points[i].emirate !== 'Kuwait'; });
    }

    map.fitBounds(L.featureGroup(focus.length ? focus : markers).getBounds().pad(0.35), {
      maxZoom: Number(el.dataset.mapMaxZoom) || 10
    });
    setTimeout(function () { map.invalidateSize(); }, 250);
  }

  function init() {
    if (typeof L === 'undefined') return;
    document.querySelectorAll('.byky-map[data-map-points]').forEach(build);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
