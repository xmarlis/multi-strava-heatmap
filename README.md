# 🏃‍♀️ Multi-Account Strava Routes + Heatmap Generator

Generate beautiful **combined route and heatmap visualizations** from multiple Strava accounts.

Perfect for couples, friends, or training groups who want to visualize shared runs, rides, or walks — each in their own color 🌈

---

## ✨ Features

- Combines activities from **multiple Strava accounts**
- Generates:
  1. **Combined Routes Map** – every route from all users
  2. **Combined Heatmap** – circles sized by activity frequency
  3. **Per-location Maps** – detailed local maps by city/region
- Uses the official Strava API with OAuth login
- Fully offline HTML maps (viewable in any browser)

---

## 🧰 Setup

### 1. Clone this repository

```bash
git clone https://github.com/YOUR_USERNAME/multi-strava-heatmap.git
cd multi-strava-heatmap
2. Create and activate a virtual environment
bash
Code kopieren
python -m venv venv
source venv/bin/activate      # macOS/Linux
venv\Scripts\activate         # Windows
3. Install dependencies
bash
Code kopieren
pip install -r requirements.txt
(If you don’t have a requirements.txt yet, just run:)

bash
Code kopieren
pip install folium requests python-dotenv polyline
pip freeze > requirements.txt
4. Set up your .env
Copy .env.example to .env:

bash
Code kopieren
cp .env.example .env
Then edit .env and insert your Strava API credentials.

Each Strava API app can only connect one athlete (in sandbox mode),
so create one app per user at Strava Developer Settings.

Example:

env
Code kopieren
STRAVA_CLIENT_ID_1=12345
STRAVA_CLIENT_SECRET_1=abcde...
STRAVA_CLIENT_ID_2=67890
STRAVA_CLIENT_SECRET_2=fghij...
🚀 Usage
Run the script:

bash
Code kopieren
python multi_strava_heatmap.py
Then follow the on-screen instructions:

Enter how many accounts you want to combine.

Authorize each Strava account in the browser (Chrome, Firefox, etc.).

The script fetches all activities and creates:

combined_routes_YYYYMMDD.html

combined_heatmap_YYYYMMDD.html

location_maps_YYYYMMDD/ (folder with local detail maps)

Open the HTML files in your browser — all maps are fully interactive.

🎨 Customization
In get_account_color() you can assign custom colors:

m → violet (#8b5cf6)

a → green (#10b981)

You can edit the color palette or popup text freely.

🧠 Notes
If you hit a “Rate Limit Exceeded” error, try again after ~15 min.

Each Strava Sandbox app allows only one athlete — use a separate app for each person.

The script stores tokens in strava_token_<name>.json to avoid re-authenticating each time.

