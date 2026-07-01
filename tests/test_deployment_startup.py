import os
from pathlib import Path
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ["DEMO_MODE"] = "false"
os.environ["SEED_DEMO"] = "false"

from app.main import STATIC_CACHE_CONTROL, app


class DeploymentStartupTest(unittest.TestCase):
    def test_base_template_uses_proxy_safe_static_stylesheet_path(self):
        with open("app/templates/base.html", encoding="utf-8") as file:
            base_template = file.read()

        self.assertIn('href="/static/css/app.css"', base_template)
        self.assertNotIn("url_for('static'", base_template)

    def test_dockerfile_runs_uvicorn_directly(self):
        with open("Dockerfile", encoding="utf-8") as file:
            dockerfile = file.read()

        self.assertIn(
            'CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]',
            dockerfile,
        )
        self.assertNotIn("scripts/start.sh", dockerfile)

    def test_static_assets_send_short_cache_control(self):
        static_app = next(route.app for route in app.routes if getattr(route, "path", None) == "/static")
        scope = {"type": "http", "method": "GET", "path": "/static", "headers": []}

        for path in (
            Path("app/static/css/app.css"),
            Path("app/static/img/crm-courier-logo.webp"),
            Path("app/static/img/favicon.svg"),
        ):
            with self.subTest(path=str(path)):
                response = static_app.file_response(str(path), path.stat(), scope)

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["cache-control"], STATIC_CACHE_CONTROL)


if __name__ == "__main__":
    unittest.main()
