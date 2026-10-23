"""Open-Meteo forecast API: winds at 850 hPa on a 1 degree grid, 72 h horizon.

Each grid point counts as one call (about 550 per run), so throttle requests over roughly
2 minutes to stay inside the free non-commercial limits.
"""
