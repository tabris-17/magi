"""Flask-free domain layer for Betelgeuse.

Modules here must never import `flask` (no `request`/`jsonify`/`render_template`)
so they can run in the background worker as well as under the web app. The web
layer (app.py) and worker.py both import from here.
"""
