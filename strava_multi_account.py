"""
Multi-Account Strava Routes + Heatmap Generator
------------------------------------------------
Kombiniert Aktivitäten aus MEHREREN Strava-Accounts in gemeinsamen Karten.
Perfekt für Paare, Freunde oder Laufgruppen.

Erzeugt:
1. Combined Routes Map  – alle Routen aller Accounts
2. Combined Heatmap     – Kreise nach Workout-Häufigkeit
3. Location Maps        – Detailkarten für jede Region

Konfiguration (.env):
- STRAVA_CLIENT_ID_1 / STRAVA_CLIENT_SECRET_1  -> App für Account 1
- STRAVA_CLIENT_ID_2 / STRAVA_CLIENT_SECRET_2  -> App für Account 2
- ...
- Optional: FROM_DATE / TO_DATE (YYYY-MM-DD) für Datumsfilter
"""

import os
import json
import webbrowser
import polyline
import folium
import requests
from dotenv import load_dotenv
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from collections import defaultdict
from datetime import datetime
from time import sleep
import time
import re
import signal
import sys


# ---------- OAuth Callback Handler ----------

class OAuthHandler(BaseHTTPRequestHandler):
    """Handle OAuth callback from Strava."""
    
    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)
        
        if 'code' in query:
            self.server.auth_code = query['code'][0]
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(
                b'<html><body><h1>Authorization successful! '
                b'You can close this window.</h1></body></html>'
            )
        else:
            self.send_response(400)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(
                b'<html><body><h1>Authorization failed!</h1></body></html>'
            )
    
    def log_message(self, format, *args):
        # keine hässlichen HTTP-Logs
        pass


# ---------- Helper für Datums-Filter ----------

def parse_date_env(name):
    """Liest FROM_DATE / TO_DATE aus .env (YYYY-MM-DD) und gibt Unix-Timestamp zurück."""
    val = os.getenv(name)
    if not val:
        return None
    try:
        dt = datetime.strptime(val.strip(), "%Y-%m-%d")
        return int(dt.timestamp())
    except ValueError:
        print(f"⚠️  Ungültiges Datumsformat in {name} (erwartet YYYY-MM-DD)")
        return None


# ---------- Token-Handling ----------

def refresh_strava_token(client_id, client_secret, token_file, token_data):
    """Refresh eines abgelaufenen Access Tokens."""
    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        return None

    print("🔄 Access token abgelaufen, versuche Refresh...")
    url = "https://www.strava.com/oauth/token"
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    response = requests.post(url, data=payload)
    if response.status_code != 200:
        print(f"❌ Token-Refresh fehlgeschlagen: {response.text}")
        return None

    new_data = response.json()
    with open(token_file, "w") as f:
        json.dump(new_data, f)

    athlete = new_data.get("athlete", {})
    print(
        f"   ✅ Token refreshed für "
        f"{athlete.get('firstname', '?')} {athlete.get('lastname', '?')} "
        f"(id={athlete.get('id')})"
    )
    return new_data["access_token"]


def authenticate_strava(client_id, client_secret, account_name):
    """OAuth-Flow + ggf. Token-Refresh für einen Account."""
    
    token_file = f"strava_token_{account_name}.json"

    # 1) vorhandenen Token wiederverwenden / refreshen
    if os.path.exists(token_file):
        with open(token_file, "r") as f:
            token_data = json.load(f)

        expires_at = token_data.get("expires_at")
        access_token = token_data.get("access_token")

        if expires_at and access_token and expires_at > time.time():
            athlete = token_data.get("athlete", {})
            print(
                f"🔑 Reuse Token für "
                f"{athlete.get('firstname', '?')} {athlete.get('lastname', '?')} "
                f"(id={athlete.get('id')})"
            )
            return access_token

        refreshed = refresh_strava_token(
            client_id, client_secret, token_file, token_data
        )
        if refreshed:
            return refreshed
        else:
            print("⚠️  Refresh fehlgeschlagen, starte vollständigen OAuth-Flow...")

    # 2) vollständiger OAuth-Flow (nur beim ersten Mal pro Account)
    redirect_uri = "http://localhost:8000/callback"
    auth_url = (
        "https://www.strava.com/oauth/authorize"
        f"?client_id={client_id}"
        "&response_type=code"
        f"&redirect_uri={redirect_uri}"
        "&scope=activity:read_all"
    )

    print(f"\n🔐 {account_name}: Strava-Login nötig")
    print("   1️⃣ Im richtigen Browser mit dem gewünschten Strava-Account einloggen")
    print("   2️⃣ Diese URL dort öffnen:")
    print(f"\n   {auth_url}\n")
    print("   (Falls sich der falsche Browser öffnet: URL einfach kopieren und im richtigen einfügen.)\n")

    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    server = HTTPServer(("localhost", 8000), OAuthHandler)
    server.auth_code = None

    print(f"⏳ Warte auf Freigabe von {account_name}...")

    while server.auth_code is None:
        server.handle_request()

    auth_code = server.auth_code

    token_url = "https://www.strava.com/oauth/token"
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": auth_code,
        "grant_type": "authorization_code",
    }

    response = requests.post(token_url, data=payload)

    if response.status_code != 200:
        print(f"❌ Token-Exchange fehlgeschlagen: {response.text}")
        return None

    token_data = response.json()

    with open(token_file, "w") as f:
        json.dump(token_data, f)

    athlete = token_data.get("athlete", {})
    print(
        f"✅ {account_name} authentifiziert als "
        f"{athlete.get('firstname', '?')} {athlete.get('lastname', '?')} "
        f"(id={athlete.get('id')})"
    )
    return token_data["access_token"]


# ---------- Aktivitäten laden ----------

def get_all_activities(access_token, account_name):
    """
    Holt Aktivitäten für einen Account.
    Nutzt optional FROM_DATE / TO_DATE aus der .env.
    """
    activities = []
    page = 1
    per_page = 100  # etwas kleiner, um Rate-Limit zu schonen

    from_ts = parse_date_env("FROM_DATE")
    to_ts = parse_date_env("TO_DATE")

    while True:
        url = "https://www.strava.com/api/v3/athlete/activities"
        headers = {"Authorization": f"Bearer {access_token}"}
        params = {"page": page, "per_page": per_page}
        if from_ts:
            params["after"] = from_ts
        if to_ts:
            params["before"] = to_ts

        response = requests.get(url, headers=headers, params=params)

        if response.status_code == 429:
            print(f"❌ Rate Limit für {account_name} überschritten (429). Später nochmal versuchen.")
            break

        if response.status_code != 200:
            try:
                data = response.json()
                if data.get("message") == "Rate Limit Exceeded":
                    print(f"❌ Rate Limit für {account_name} überschritten (API-Meldung).")
                    break
            except Exception:
                pass

            print(f"❌ Fehler beim Laden für {account_name}: {response.text}")
            break

        page_activities = response.json()
        if not page_activities:
            break

        for activity in page_activities:
            activity["_account"] = account_name

        activities.extend(page_activities)
        print(f"📥 {len(activities)} Aktivitäten von {account_name} geladen...")

        if len(page_activities) < per_page:
            break

        page += 1

    return activities


# ---------- Geocoding / Farben ----------

def get_continent_from_country(country):
    """Map country to continent."""
    continent_mapping = {
        # Europe
        "Germany": "Europe", "France": "Europe", "Italy": "Europe", "Spain": "Europe",
        "United Kingdom": "Europe", "Netherlands": "Europe", "Belgium": "Europe",
        "Switzerland": "Europe", "Austria": "Europe", "Portugal": "Europe",
        "Greece": "Europe", "Poland": "Europe", "Czech Republic": "Europe",
        "Sweden": "Europe", "Norway": "Europe", "Denmark": "Europe", "Finland": "Europe",
        "Ireland": "Europe", "Croatia": "Europe", "Slovenia": "Europe", "Hungary": "Europe",
        "Romania": "Europe", "Bulgaria": "Europe", "Serbia": "Europe", "Slovakia": "Europe",
        "Iceland": "Europe",
        
        # North America (includes Central America)
        "United States": "North America", "United States of America": "North America",
        "Canada": "North America", "Mexico": "North America",
        "El Salvador": "North America", "Costa Rica": "North America", 
        "Panama": "North America", "Guatemala": "North America", "Honduras": "North America",
        "Nicaragua": "North America", "Belize": "North America",
        
        # South America
        "Brazil": "South America", "Argentina": "South America", "Chile": "South America",
        "Peru": "South America", "Colombia": "South America", "Venezuela": "South America",
        "Ecuador": "South America", "Bolivia": "South America", "Paraguay": "South America",
        "Uruguay": "South America", "Guyana": "South America", "Suriname": "South America",
        
        # Asia
        "China": "Asia", "Japan": "Asia", "India": "Asia", "Thailand": "Asia",
        "Vietnam": "Asia", "Indonesia": "Asia", "Malaysia": "Asia", "Singapore": "Asia",
        "South Korea": "Asia", "Taiwan": "Asia", "Philippines": "Asia",
        
        # Africa
        "South Africa": "Africa", "Egypt": "Africa", "Morocco": "Africa",
        "Kenya": "Africa", "Tanzania": "Africa", "Nigeria": "Africa",
        
        # Oceania
        "Australia": "Oceania", "New Zealand": "Oceania", "Fiji": "Oceania",
    }
    return continent_mapping.get(country, "Unknown")


def get_city_from_coordinates(lat, lng, cache={}):
    """Reverse Geocoding via Nominatim mit einfachem Cache."""
    cache_key = f"{round(lat, 1)},{round(lng, 1)}"
    
    if cache_key in cache:
        return cache[cache_key]
    
    try:
        url = "https://nominatim.openstreetmap.org/reverse"
        params = {
            "lat": lat,
            "lon": lng,
            "format": "json",
            "zoom": 10,
            "addressdetails": 1,
            "accept-language": "en",
        }
        headers = {"User-Agent": "StravaMultiAccountHeatmap/1.0"}
        
        response = requests.get(url, params=params, headers=headers, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            address = data.get("address", {})
            
            city = (
                address.get("city")
                or address.get("town")
                or address.get("village")
                or address.get("municipality")
                or address.get("county")
            )
            country = address.get("country", "")
            
            if city and country:
                result = f"{city}, {country}"
            elif country:
                result = country
            elif city:
                result = city
            else:
                result = None
            
            cache[cache_key] = result
            sleep(1)
            return result
    except KeyboardInterrupt:
        raise
    except Exception:
        pass
    
    cache[cache_key] = None
    return None


def get_account_color(account_name, account_colors):
    """Fixed color per account (m = violet, a = green, others from palette)."""
    # Hard-code special colors
    lower = account_name.lower()
    if lower == "m":
        account_colors[account_name] = "#8b5cf6"  # violet
        return account_colors[account_name]
    if lower == "a":
        account_colors[account_name] = "#10b981"  # green
        return account_colors[account_name]
    if lower == "o":
        account_colors[account_name] = "#f97316"  # orange  ← NEU für "o"
        return account_colors[account_name]

    # Default behavior for any other account
    if account_name not in account_colors:
        colors = [
            "#ef4444",
            "#3b82f6",
            "#10b981",
            "#f59e0b",
            "#8b5cf6",
            "#ec4899",
            "#06b6d4",
            "#84cc16",
            "#f97316",
            "#6366f1",
        ]
        account_colors[account_name] = colors[len(account_colors) % len(colors)]
    return account_colors[account_name]


def get_location_key(activity, geocode_cache={}):
    """String-Schlüssel pro Region (City, Country oder Koordinaten)."""
    city = activity.get("location_city", "")
    country = activity.get("location_country", "")
    
    if city and country:
        return f"{city}, {country}"
    elif country:
        return country
    elif city:
        return city
    
    start_latlng = activity.get("start_latlng")
    if start_latlng:
        lat = start_latlng[0]
        lng = start_latlng[1]
        
        location_name = get_city_from_coordinates(lat, lng, geocode_cache)
        if location_name:
            return location_name
        
        lat_rounded = round(lat, 1)
        lng_rounded = round(lng, 1)
        return f"Location ({lat_rounded}°, {lng_rounded}°)"
    
    return "Unknown"


def assign_region_color(location_key):
    """Farbe pro Region."""
    colors = [
        "#ef4444",
        "#f59e0b",
        "#eab308",
        "#84cc16",
        "#22c55e",
        "#10b981",
        "#14b8a6",
        "#06b6d4",
        "#0ea5e9",
        "#3b82f6",
        "#6366f1",
        "#8b5cf6",
        "#a855f7",
        "#d946ef",
        "#ec4899",
        "#f43f5e",
        "#fb923c",
        "#fbbf24",
        "#a3e635",
        "#4ade80",
    ]
    hash_val = hash(location_key)
    return colors[hash_val % len(colors)]


# ---------- Kartenbau: Locations ----------

def create_location_routes_map(location_key, activities, timestamp, folder_name, accounts):
    """Erstellt Detailkarte für eine Region mit allen Accounts."""
    
    safe_name = re.sub(r"[^\w\s-]", "", location_key).strip().replace(" ", "_")[:50]
    
    valid_activities = [a for a in activities if a.get("start_latlng")]
    if not valid_activities:
        return None
    
    center_lat = sum(a["start_latlng"][0] for a in valid_activities) / len(valid_activities)
    center_lng = sum(a["start_latlng"][1] for a in valid_activities) / len(valid_activities)
    
    map_obj = folium.Map(
        location=[center_lat, center_lng],
        zoom_start=13,
        tiles="CartoDB positron",
    )
    
    account_stats = defaultdict(
        lambda: {"runs": 0, "walks": 0, "rides": 0, "total": 0}
    )
    account_colors = {}
    routes_added = 0
    
    for activity in activities:
        polyline_str = activity.get("map", {}).get("summary_polyline")
        
        if polyline_str:
            try:
                coords = polyline.decode(polyline_str)
                activity_type = activity.get("type", "Unknown").lower()
                account_name = activity.get("_account", "Unknown")
                
                color = get_account_color(account_name, account_colors)
                
                account_stats[account_name]["total"] += 1
                if activity_type == "run":
                    account_stats[account_name]["runs"] += 1
                elif activity_type == "walk":
                    account_stats[account_name]["walks"] += 1
                elif activity_type == "ride":
                    account_stats[account_name]["rides"] += 1
                
                folium.PolyLine(
                    coords,
                    color=color,
                    weight=2,
                    opacity=0.6,
                    popup=f"{account_name}: {activity.get('name', 'Activity')}",
                ).add_to(map_obj)
                
                routes_added += 1
            except Exception:
                pass
    
    legend_items = ""
    for account_name in accounts:
        if account_name in account_stats:
            stats = account_stats[account_name]
            color = get_account_color(account_name, account_colors)
            legend_items += f"""
            <div style="margin-bottom: 12px;">
                <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 4px;">
                    <div style="width: 12px; height: 12px; background: {color}; border-radius: 50%;"></div>
                    <span style="font-size: 13px; font-weight: 600; color: #1e293b;">{account_name}</span>
                </div>
                <div style="font-size: 11px; color: #64748b; margin-left: 20px;">
                    R:{stats['runs']} W:{stats['walks']} B:{stats['rides']}
                </div>
            </div>
            """
    
    legend_html = f"""
    <div style="position: fixed; 
                bottom: 30px; right: 30px; 
                width: 220px; 
                background: white;
                border-radius: 12px;
                box-shadow: 0 4px 24px rgba(0,0,0,0.15);
                z-index: 9999;
                padding: 20px;
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;">
        <div style="font-size: 16px; 
                   font-weight: 700;
                   margin-bottom: 8px;
                   color: #1e293b;">
            📍 {location_key}
        </div>
        <div style="font-size: 13px; 
                   color: #64748b;
                   margin-bottom: 16px;">
            {routes_added} routes
        </div>
        {legend_items}
    </div>
    """
    map_obj.get_root().html.add_child(folium.Element(legend_html))
    
    filename = f"location_{safe_name}.html"
    output_file = os.path.join(folder_name, filename)
    map_obj.save(output_file)
    
    return filename


# ---------- Kartenbau: Combined Routes ----------

def create_combined_routes_map(all_activities, timestamp, accounts):
    """Große Routenkarte mit allen Accounts."""
    
    filtered_activities = [
        a
        for a in all_activities
        if a.get("type", "").lower() in ["run", "walk", "ride"]
    ]
    
    if not filtered_activities:
        print("❌ Keine Aktivitäten mit GPS-Daten gefunden")
        return None
    
    first_activity_with_location = None
    for activity in filtered_activities:
        if activity.get("start_latlng"):
            first_activity_with_location = activity
            break
    
    if not first_activity_with_location:
        print("❌ Keine Aktivitäten mit GPS-Daten gefunden")
        return None
    
    center_lat, center_lng = first_activity_with_location["start_latlng"]
    map_obj = folium.Map(
        location=[center_lat, center_lng],
        zoom_start=12,
        tiles="CartoDB positron",
    )
    
    account_stats = defaultdict(
        lambda: {"runs": 0, "walks": 0, "rides": 0, "total": 0}
    )
    account_colors = {}
    routes_added = 0
    
    print("🗺️  Füge Routen zur Combined Map hinzu...")
    
    for activity in filtered_activities:
        polyline_str = activity.get("map", {}).get("summary_polyline")
        
        if polyline_str:
            try:
                coords = polyline.decode(polyline_str)
                activity_type = activity.get("type", "Unknown").lower()
                account_name = activity.get("_account", "Unknown")
                
                color = get_account_color(account_name, account_colors)
                
                account_stats[account_name]["total"] += 1
                if activity_type == "run":
                    account_stats[account_name]["runs"] += 1
                elif activity_type == "walk":
                    account_stats[account_name]["walks"] += 1
                elif activity_type == "ride":
                    account_stats[account_name]["rides"] += 1
                
                # Add data attributes for filtering
                folium.PolyLine(
                    coords,
                    color=color,
                    weight=2,
                    opacity=0.6,
                    popup=f"{account_name}: {activity.get('name', 'Activity')} ({activity_type})",
                    className=f"route-{account_name.lower().replace(' ', '-')}"

                ).add_to(map_obj)
                
                routes_added += 1
                
            except Exception as e:
                print(f"⚠️  Aktivität übersprungen: {e}")
    
    legend_items = ""
    for account_name in accounts:
        stats = account_stats[account_name]
        color = get_account_color(account_name, account_colors)
        account_id = account_name.lower().replace(' ', '-')
        legend_items += f"""
        <div style="margin-bottom: 16px; padding-bottom: 16px; border-bottom: 1px solid #e2e8f0;">
            <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 8px;">
                <div style="width: 16px; height: 16px; background: {color}; border-radius: 50%;"></div>
                <span style="font-size: 15px; font-weight: 600; color: #1e293b;">{account_name}</span>
                <button onclick="toggleAccount('{account_id}')" id="btn-{account_id}"
                        style="margin-left: auto; padding: 4px 8px; 
                               background: {color}; color: white; 
                               border: none; border-radius: 4px; 
                               cursor: pointer; font-size: 11px; font-weight: 600;">
                    HIDE
                </button>
            </div>
            <div style="font-size: 13px; color: #64748b; margin-left: 24px;">
                <div>Runs: {stats['runs']}</div>
                <div>Walks: {stats['walks']}</div>
                <div>Rides: {stats['rides']}</div>
                <div style="margin-top: 4px; font-weight: 600;">Total: {stats['total']}</div>
            </div>
        </div>
        """
    
    # Add JavaScript for filtering
    filter_script = """
    <script>
    var accountVisibility = {};
    
    function toggleAccount(accountId) {
        var elements = document.querySelectorAll('.route-' + accountId);
        var button = document.getElementById('btn-' + accountId);
        var isVisible = accountVisibility[accountId] !== false;
        
        elements.forEach(function(el) {
            el.style.display = isVisible ? 'none' : '';
        });
        
        accountVisibility[accountId] = !isVisible;
        button.textContent = isVisible ? 'SHOW' : 'HIDE';
        button.style.opacity = isVisible ? '0.5' : '1';
    }
    </script>
    """
    
    legend_html = f"""
    <div style="position: fixed; 
                bottom: 30px; right: 30px; 
                width: 240px; 
                max-height: 80vh;
                overflow-y: auto;
                background: white;
                border-radius: 12px;
                box-shadow: 0 4px 24px rgba(0,0,0,0.15);
                z-index: 9999;
                padding: 20px;
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;">
        <div style="font-size: 16px; 
                   font-weight: 700;
                   margin-bottom: 16px;
                   color: #1e293b;">
            🤝 Combined Journey
        </div>
        {legend_items}
        <div style="font-size: 12px; color: #94a3b8; margin-top: 12px;">
            Total routes: {routes_added}
        </div>
    </div>
    {filter_script}
    """
    map_obj.get_root().html.add_child(folium.Element(legend_html))
    
    output_file = f"combined_routes_{timestamp}.html"
    map_obj.save(output_file)
    
    print(f"\n✅ Combined Routes Map: {output_file}")
    print(f"   📊 {routes_added} Routen")
    for account_name in accounts:
        stats = account_stats[account_name]
        print(f"   👤 {account_name}: {stats['total']} Aktivitäten")
    
    return output_file


# ---------- Kartenbau: Combined Heatmap ----------

def create_combined_heatmap(all_activities, timestamp, accounts):
    """Heatmap mit Kreisen pro Region + Klick auf Detailkarte."""
    
    filtered_activities = [
        a
        for a in all_activities
        if a.get("type", "").lower() in ["run", "walk", "ride"]
    ]
    
    location_data = defaultdict(
        lambda: {
            "count": 0,
            "lat": 0,
            "lng": 0,
            "activities": [],
            "by_account": defaultdict(int),
        }
    )
    
    geocode_cache = {}
    
    # Track continents and countries
    continents_visited = set()
    countries_visited = set()
    
    print("🌍 Verarbeite Locations (City-Namen)...")
    
    for idx, activity in enumerate(filtered_activities, 1):
        if idx % 100 == 0:
            print(f"   {idx}/{len(filtered_activities)} Aktivitäten...")
        
        start_latlng = activity.get("start_latlng")
        if not start_latlng:
            continue
        
        location_key = get_location_key(activity, geocode_cache)
        account_name = activity.get("_account", "Unknown")
        
        # Extract country from location_key - FIXED VERSION
        country = None
        if ", " in location_key:
            # "City, Country" format
            parts = location_key.split(", ")
            country = parts[-1]
        else:
            # Check if the entire location_key is itself a country
            test_continent = get_continent_from_country(location_key)
            if test_continent != "Unknown":
                country = location_key
        
        if country:
            countries_visited.add(country)
            continent = get_continent_from_country(country)
            if continent != "Unknown":
                continents_visited.add(continent)
        
        location_data[location_key]["count"] += 1
        location_data[location_key]["lat"] = start_latlng[0]
        location_data[location_key]["lng"] = start_latlng[1]
        location_data[location_key]["activities"].append(activity)
        location_data[location_key]["by_account"][account_name] += 1
    
    print("\n🗺️  Erzeuge Location-Detailkarten...")
    location_folder = f"location_maps_{timestamp}"
    os.makedirs(location_folder, exist_ok=True)
    
    location_map_files = {}
    for idx, (location_key, data) in enumerate(location_data.items(), 1):
        if idx % 10 == 0:
            print(f"   {idx}/{len(location_data)} Locations...")
        
        location_file = create_location_routes_map(
            location_key, data["activities"], timestamp, location_folder, accounts
        )
        if location_file:
            location_map_files[location_key] = location_file
    
    print(f"   ✅ {len(location_map_files)} Location-Maps erstellt")
    
    map_obj = folium.Map(
        location=[20, 0],
        zoom_start=2,
        tiles="CartoDB positron",
        world_copy_jump=True,
    )
    
    print("\n🗺️  Erzeuge Heatmap-Kreise...")
    
    counts = [data["count"] for data in location_data.values()]
    min_count = min(counts) if counts else 1
    max_count = max(counts) if counts else 1
    
    for location_key, data in location_data.items():
        count = data["count"]
        lat = data["lat"]
        lng = data["lng"]
        
        if max_count > min_count:
            normalized = (count - min_count) / (max_count - min_count)
        else:
            normalized = 1
        
        radius = 400000 + (normalized * 600000)
        color = assign_region_color(location_key)
        
        account_breakdown = "<br>".join(
            [f"{acc}: {cnt}" for acc, cnt in data["by_account"].items()]
        )
        
        location_file = location_map_files.get(location_key)
        click_hint = "<br><br>🖱️ <i>Click to view routes</i>" if location_file else ""
        
        tooltip = f"""
        <b>{location_key}</b><br>
        Total: {count} workouts<br>
        <br>
        {account_breakdown}{click_hint}
        """
        
        circle = folium.Circle(
            location=[lat, lng],
            radius=radius,
            color=color,
            fill=True,
            fillColor=color,
            fillOpacity=0.6,
            weight=2,
            tooltip=tooltip,
        )
        
        if location_file:
            relative_path = f"./{location_folder}/{location_file}"
            circle.add_child(
                folium.Popup(
                    html=f"""
                <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; padding: 10px;">
                    <h3 style="margin: 0 0 10px 0;">{location_key}</h3>
                    <p style="margin: 5px 0;"><strong>{count} workouts</strong></p>
                    <div style="margin: 8px 0; font-size: 13px;">{account_breakdown.replace("<br>", "<br/>")}</div>
                    <button onclick="window.open('{relative_path}', '_blank')" 
                            style="margin-top: 10px; padding: 8px 16px; 
                                   background: #3b82f6; color: white; 
                                   border: none; border-radius: 6px; 
                                   cursor: pointer; font-weight: 600;">
                        📍 View All Routes
                    </button>
                </div>
            """,
                    max_width=300,
                )
            )
        
        circle.add_to(map_obj)
    
    account_stats = defaultdict(
        lambda: {"runs": 0, "walks": 0, "rides": 0, "total": 0}
    )
    for activity in filtered_activities:
        account_name = activity.get("_account", "Unknown")
        activity_type = activity.get("type", "").lower()
        account_stats[account_name]["total"] += 1
        if activity_type == "run":
            account_stats[account_name]["runs"] += 1
        elif activity_type == "walk":
            account_stats[account_name]["walks"] += 1
        elif activity_type == "ride":
            account_stats[account_name]["rides"] += 1
    
    # Create continents/countries display
    continents_list = sorted(list(continents_visited)) if continents_visited else ["None yet"]
    countries_list = sorted(list(countries_visited)) if countries_visited else ["None yet"]
    
    continents_html = f"""
    <div style="margin-bottom: 20px; padding: 16px; background: #f8fafc; border-radius: 8px;">
        <div style="font-size: 14px; font-weight: 600; color: #1e293b; margin-bottom: 8px;">
            🌍 Continents Visited
        </div>
        <div style="font-size: 12px; color: #64748b; line-height: 1.6;">
            {', '.join(continents_list)} ({len(continents_visited)})
        </div>
    </div>
    
    <div style="margin-bottom: 20px; padding: 16px; background: #f8fafc; border-radius: 8px;">
        <div style="font-size: 14px; font-weight: 600; color: #1e293b; margin-bottom: 8px;">
            🗺️ Countries Visited
        </div>
        <div style="font-size: 12px; color: #64748b; line-height: 1.6;">
            {', '.join(countries_list)} ({len(countries_visited)})
        </div>
    </div>
    """
    
    account_colors = {}
    account_stats_html = ""
    for account_name in accounts:
        stats = account_stats[account_name]
        color = get_account_color(account_name, account_colors)
        account_stats_html += f"""
        <div style="border-top: 1px solid #e2e8f0; 
                   padding-top: 16px;
                   margin-top: 16px;">
            <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 12px;">
                <div style="width: 16px; height: 16px; background: {color}; border-radius: 50%;"></div>
                <div style="font-size: 15px; font-weight: 600; color: #1e293b;">{account_name}</div>
            </div>
            <div style="display: flex; flex-direction: column; gap: 6px; margin-left: 24px;">
                <div style="font-size: 13px; color: #64748b;">
                    Runs: <strong>{stats['runs']}</strong>
                </div>
                <div style="font-size: 13px; color: #64748b;">
                    Walks: <strong>{stats['walks']}</strong>
                </div>
                <div style="font-size: 13px; color: #64748b;">
                    Rides: <strong>{stats['rides']}</strong>
                </div>
                <div style="font-size: 14px; color: #1e293b; margin-top: 4px;">
                    Total: <strong>{stats['total']}</strong>
                </div>
            </div>
        </div>
        """
    
    sidebar_html = f"""
    <div style="position: fixed; 
                top: 20px; left: 20px; 
                width: 280px; 
                max-height: calc(100vh - 40px);
                background: white;
                border-radius: 16px;
                box-shadow: 0 4px 24px rgba(0,0,0,0.15);
                z-index: 9999;
                padding: 24px;
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                overflow-y: auto;">
        
        <div style="margin-bottom: 20px;">
            <h2 style="margin: 0 0 8px 0; 
                       font-size: 24px; 
                       font-weight: 700;
                       color: #1e293b;">
                🤝 Combined Journey
            </h2>
            <p style="margin: 0;
                      font-size: 12px;
                      color: #64748b;">
                {len(accounts)} accounts • {len(filtered_activities)} workouts
            </p>
        </div>
        
        <div>
            <div style="font-size: 36px; 
                       font-weight: 700; 
                       color: #3b82f6;
                       line-height: 1;">
                {len(location_data)}
            </div>
            <div style="font-size: 13px; 
                       color: #64748b;
                       margin-top: 4px;">
                unique locations
            </div>
        </div>
        
        {continents_html}
        
        {account_stats_html}
        
        <div style="border-top: 1px solid #e2e8f0; 
                   margin-top: 20px;
                   padding-top: 16px;
                   font-size: 11px;
                   color: #94a3b8;">
            🖱️ Click circles to view detailed routes
        </div>
    </div>
    """
    
    map_obj.get_root().html.add_child(folium.Element(sidebar_html))
    
    output_file = f"combined_heatmap_{timestamp}.html"
    map_obj.save(output_file)
    
    print(f"\n✅ Combined Heatmap: {output_file}")
    print(f"   📊 {len(location_data)} Locations")
    print(f"   🌍 {len(continents_visited)} Continents")
    print(f"   🗺️  {len(countries_visited)} Countries")
    print(f"   🔗 {len(location_map_files)} klickbare Location-Maps")
    
    return output_file


# ---------- main() ----------

def main():
    """Hauptfunktion."""
    
    def signal_handler(sig, frame):
        print("\n\n⚠️  Script abgebrochen.")
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    
    load_dotenv()
    
    print("=" * 70)
    print("🤝 Multi-Account Strava Routes + Heatmap Generator")
    print("=" * 70)
    print()
    
    while True:
        try:
            num_accounts = int(
                input("How many Strava accounts do you want to combine? (1-5): ").strip()
            )
            if 1 <= num_accounts <= 5:
                break
            print("⚠️  Bitte Zahl zwischen 1 und 5 eingeben")
        except ValueError:
            print("⚠️  Bitte eine gültige Zahl eingeben")
    
    accounts = []
    account_credentials = []
    
    for i in range(num_accounts):
        print(f"\n📝 Account {i+1} von {num_accounts}")
        print("-" * 40)
        
        account_name = input(
            "Enter a name for this account (e.g., 'm', 'a', 'o'): "
        ).strip()
        if not account_name:
            account_name = f"Account{i+1}"
        
        # STRAVA_CLIENT_ID_1, STRAVA_CLIENT_ID_2, ...
        client_id = os.getenv(f"STRAVA_CLIENT_ID_{i+1}") or os.getenv("STRAVA_CLIENT_ID")
        client_secret = os.getenv(f"STRAVA_CLIENT_SECRET_{i+1}") or os.getenv(
            "STRAVA_CLIENT_SECRET"
        )
        
        if not client_id:
            client_id = input("  Client ID: ").strip()
        if not client_secret:
            client_secret = input("  Client Secret: ").strip()
        
        if not client_id or not client_secret:
            print(f"❌ Fehlende Credentials für {account_name}")
            return
        
        accounts.append(account_name)
        account_credentials.append(
            {"name": account_name, "client_id": client_id, "client_secret": client_secret}
        )
    
    print("\n" + "=" * 70)
    print("🔄 Authentifiziere Accounts...")
    print("=" * 70)
    
    all_activities = []
    
    for creds in account_credentials:
        print(f"\n👤 Verarbeite {creds['name']}...")
        
        access_token = authenticate_strava(
            creds["client_id"], creds["client_secret"], creds["name"]
        )
        
        if not access_token:
            print(f"❌ Authentifizierung fehlgeschlagen für {creds['name']}")
            continue
        
        print(f"📡 Lade Aktivitäten für {creds['name']}...")
        activities = get_all_activities(access_token, creds["name"])
        
        if activities:
            all_activities.extend(activities)
            print(f"✅ {len(activities)} Aktivitäten von {creds['name']}")
        else:
            print(f"⚠️  Keine Aktivitäten gefunden für {creds['name']}")
    
    if not all_activities:
        print("\n❌ Von keinem Account wurden Aktivitäten geladen (Rate-Limit? Datum zu streng?)")
        return
    
    print(
        f"\n✅ Gesamt: {len(all_activities)} Aktivitäten "
        f"aus {len(accounts)} Accounts"
    )
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    print("\n" + "=" * 70)
    print("📍 Erzeuge Combined Routes Map...")
    print("=" * 70)
    routes_file = create_combined_routes_map(all_activities, timestamp, accounts)
    
    print("\n" + "=" * 70)
    print("🌍 Erzeuge Combined Heatmap...")
    print("=" * 70)
    heatmap_file = create_combined_heatmap(all_activities, timestamp, accounts)
    
    print("\n" + "=" * 70)
    print("🎉 Fertig!")
    print("=" * 70)
    print(f"\n📁 Ausgaben:")
    print(f"   1. Routes: {routes_file}")
    print(f"   2. Heatmap: {heatmap_file}")
    print(f"   3. Location Maps: location_maps_{timestamp}/")
    print("\n🌐 Öffne die Heatmap-HTML-Datei in deinem Browser.")
    print("💡 Jede Person hat ihre eigene Farbe.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⚠️  Script interrupted (Ctrl+C).")
        sys.exit(0)
    except Exception as e:
        print(f"\n\n❌ Fehler: {e}")
        sys.exit(1)