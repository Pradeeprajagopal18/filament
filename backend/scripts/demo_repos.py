"""Build sample repos of each kind, run the real analyzer on them, and write the results for the
interactive preview (frontend/src/demo-repos.json). Re-run after changing the analyzers:

    uv run python scripts/demo_repos.py
"""

import json
import sys
import tempfile
from pathlib import Path

import yaml

from filament.repos.analyze import analyze_path
from filament.repos.generate import build, conventions

REPOS: dict[str, dict[str, str]] = {
    "github.com/acme/checkout-api": {
        "README.md": "# checkout-api\nCart, order and payment APIs for the storefront.\n",
        "pyproject.toml": '[project]\nname = "checkout-api"\ndependencies = ["fastapi", "sqlalchemy", '
                          '"psycopg", "redis", "stripe", "aiokafka", "sentry-sdk"]\n\n'
                          '[tool.ruff]\nline-length = 100\n',
        "uv.lock": "version = 1\n",
        "Makefile": "dev:\n\tuvicorn app.main:app --reload\ntest:\n\tpytest\nlint:\n\truff check .\n",
        ".github/workflows/ci.yml": "on: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
                                    "    steps:\n      - run: make test\n",
        "Dockerfile": "FROM python:3.12-slim\n",
        "docker-compose.yml": "services:\n  api:\n    build: .\n    depends_on: [db, cache]\n"
                              "    environment:\n      INVENTORY_SERVICE_URL: http://inventory:8080\n"
                              "  db:\n    image: postgres:16\n  cache:\n    image: redis:7\n",
        "app/main.py": (
            "import os\nimport sentry_sdk\nfrom fastapi import FastAPI\n"
            "from app.routers import carts, orders, payments\n\n"
            "sentry_sdk.init(os.environ.get('SENTRY_DSN'))\napp = FastAPI(title='checkout-api')\n"
            "app.include_router(carts.router, prefix='/v1')\n"
            "app.include_router(orders.router, prefix='/v1')\n"
            "app.include_router(payments.router, prefix='/v1')\n\n"
            "@app.get('/health')\ndef health():\n    \"\"\"Liveness probe.\"\"\"\n    return {'ok': True}\n"
        ),
        "app/routers/carts.py": (
            "from fastapi import APIRouter\nrouter = APIRouter(prefix='/carts', tags=['carts'])\n\n"
            "@router.post('')\ndef create_cart():\n    \"\"\"Start a cart for a shopper.\"\"\"\n\n"
            "@router.get('/{cart_id}')\ndef get_cart(cart_id: str):\n    \"\"\"Read a cart with line items.\"\"\"\n\n"
            "@router.post('/{cart_id}/items')\ndef add_item(cart_id: str):\n    \"\"\"Add a product to the cart.\"\"\"\n\n"
            "@router.delete('/{cart_id}/items/{item_id}')\ndef remove_item(cart_id: str, item_id: str):\n"
            "    \"\"\"Remove a line item.\"\"\"\n"
        ),
        "app/routers/orders.py": (
            "import os\nfrom fastapi import APIRouter\nrouter = APIRouter(prefix='/orders', tags=['orders'])\n"
            "KAFKA = os.environ['KAFKA_BROKERS']\n\n"
            "@router.post('')\ndef place_order():\n    \"\"\"Turn a cart into an order and emit order.placed.\"\"\"\n\n"
            "@router.get('')\ndef list_orders(customer_id: str):\n    \"\"\"Orders for a customer.\"\"\"\n\n"
            "@router.get('/{order_id}')\ndef get_order(order_id: str):\n    \"\"\"Read one order.\"\"\"\n\n"
            "@router.post('/{order_id}/cancel')\ndef cancel_order(order_id: str):\n    \"\"\"Cancel before fulfilment.\"\"\"\n"
        ),
        "app/routers/payments.py": (
            "import stripe\nfrom fastapi import APIRouter\n"
            "router = APIRouter(prefix='/payments', tags=['payments'])\n"
            "STRIPE_API = 'https://api.stripe.com/v1/payment_intents'\n\n"
            "@router.post('/intents')\ndef create_intent():\n    \"\"\"Create a Stripe payment intent for an order.\"\"\"\n\n"
            "@router.post('/webhooks/stripe')\ndef stripe_webhook():\n    \"\"\"Payment status from Stripe.\"\"\"\n\n"
            "@router.post('/{payment_id}/refund')\ndef refund(payment_id: str):\n    \"\"\"Refund a captured payment.\"\"\"\n"
        ),
        "app/db.py": "import os\nfrom sqlalchemy import create_engine\n"
                     "engine = create_engine(os.environ['DATABASE_URL'])\n",
        "app/cache.py": "import os, redis\nr = redis.from_url(os.environ['REDIS_URL'])\n",
        "tests/test_carts.py": "def test_create_cart():\n    assert True\n",
    },
    "github.com/acme/web-storefront": {
        "README.md": "# web-storefront\nThe shopper-facing web app.\n",
        "package.json": json.dumps({
            "name": "web-storefront", "scripts": {"dev": "vite", "build": "vite build",
                                                  "test": "vitest", "lint": "eslint ."},
            "dependencies": {"react": "19", "react-dom": "19", "react-router-dom": "7",
                             "@stripe/stripe-js": "4", "@sentry/react": "8"},
            "devDependencies": {"vite": "6", "vitest": "2", "eslint": "9", "typescript": "5"}}),
        "package-lock.json": "{}",
        "eslint.config.js": "export default []\n",
        "src/main.tsx": "import { createRoot } from 'react-dom/client'\nimport App from './App'\n"
                        "createRoot(document.getElementById('root')!).render(<App />)\n",
        "src/api.ts": "const BASE = process.env.REACT_APP_CHECKOUT_API_URL\n"
                      "export const getCart = (id: string) => fetch(`${BASE}/v1/carts/${id}`)\n",
        "src/App.tsx": (
            "import { BrowserRouter, Routes, Route } from 'react-router-dom'\n"
            "import Home from './pages/Home'\nimport Product from './pages/Product'\n"
            "import Cart from './pages/Cart'\nimport Checkout from './pages/Checkout'\n"
            "import Account from './pages/Account'\n\n"
            "export default function App() {\n  return (\n    <BrowserRouter>\n      <Routes>\n"
            "        <Route path=\"/\" element={<Home />} />\n"
            "        <Route path=\"/products/:id\" element={<Product />} />\n"
            "        <Route path=\"/cart\" element={<Cart />} />\n"
            "        <Route path=\"/checkout\" element={<Checkout />} />\n"
            "        <Route path=\"/account\" element={<Account />} />\n"
            "      </Routes>\n    </BrowserRouter>\n  )\n}\n"
        ),
        "src/components/Header.tsx": (
            "export default function Header() {\n  return (\n    <header>\n      <nav>\n"
            "        <a href=\"/\">Acme Store</a>\n        <input placeholder=\"Search products\" />\n"
            "        <a href=\"/cart\">Cart</a>\n        <a href=\"/account\">Account</a>\n"
            "      </nav>\n    </header>\n  )\n}\n"
        ),
        "src/components/ProductCard.tsx": (
            "export default function ProductCard({ p }) {\n  return (\n    <div>\n"
            "      <img alt=\"Product photo\" />\n      <h3>Product name</h3>\n"
            "      <p>Price</p>\n      <button>Add to cart</button>\n    </div>\n  )\n}\n"
        ),
        "src/pages/Home.tsx": (
            "import Header from '../components/Header'\nimport ProductCard from '../components/ProductCard'\n"
            "export default function Home() {\n  return (\n    <main>\n      <Header />\n"
            "      <h1>New this week</h1>\n      <p>Fresh picks from our makers.</p>\n"
            "      <button>Shop all</button>\n      <section>\n"
            "        {products.map(p => <ProductCard p={p} />)}\n      </section>\n    </main>\n  )\n}\n"
        ),
        "src/pages/Product.tsx": (
            "import Header from '../components/Header'\n"
            "export default function Product() {\n  return (\n    <main>\n      <Header />\n"
            "      <img alt=\"Product gallery\" />\n      <h1>Product name</h1>\n      <p>Description</p>\n"
            "      <select><option>Size</option></select>\n      <input type=\"number\" placeholder=\"Qty\" />\n"
            "      <button>Add to cart</button>\n    </main>\n  )\n}\n"
        ),
        "src/pages/Cart.tsx": (
            "import Header from '../components/Header'\n"
            "export default function Cart() {\n  return (\n    <main>\n      <Header />\n"
            "      <h1>Your cart</h1>\n      <table>\n        {items.map(i => <tr><td>{i.name}</td></tr>)}\n"
            "      </table>\n      <input placeholder=\"Promo code\" />\n      <button>Apply</button>\n"
            "      <a href=\"/checkout\">Go to checkout</a>\n    </main>\n  )\n}\n"
        ),
        "src/pages/Checkout.tsx": (
            "import Header from '../components/Header'\n"
            "export default function Checkout() {\n  return (\n    <main>\n      <Header />\n"
            "      <h1>Checkout</h1>\n      <form>\n        <h2>Shipping</h2>\n"
            "        <input placeholder=\"Full name\" />\n        <input placeholder=\"Address\" />\n"
            "        <input placeholder=\"Postcode\" />\n        <h2>Payment</h2>\n"
            "        <input placeholder=\"Card number\" />\n"
            "        <label><input type=\"checkbox\" /> Save card for next time</label>\n"
            "        <button>Place order</button>\n      </form>\n    </main>\n  )\n}\n"
        ),
        "src/pages/Account.tsx": (
            "import Header from '../components/Header'\n"
            "export default function Account() {\n  return (\n    <main>\n      <Header />\n"
            "      <h1>Your account</h1>\n      <h2>Orders</h2>\n"
            "      <ul>\n        {orders.map(o => <li>{o.id}</li>)}\n      </ul>\n"
            "      <h2>Saved cards</h2>\n      <button>Add card</button>\n      <button>Sign out</button>\n"
            "    </main>\n  )\n}\n"
        ),
        "src/pages/Home.test.tsx": "test('renders', () => {})\n",
    },
    "github.com/acme/platform-infra": {
        "README.md": "# platform-infra\nShared AWS infrastructure for the storefront.\n",
        "main.tf": (
            'terraform {\n  required_version = ">= 1.7"\n  required_providers {\n'
            '    aws = { source = "hashicorp/aws", version = "~> 5.0" }\n  }\n}\n\n'
            'module "vpc" {\n  source = "./modules/vpc"\n  cidr   = "10.20.0.0/16"\n}\n\n'
            'resource "aws_eks_cluster" "shop" {\n  name = "shop"\n'
            '  vpc_config { subnet_ids = module.vpc.private_subnets }\n}\n\n'
            'resource "aws_db_instance" "checkout" {\n  engine = "postgres"\n'
            '  db_subnet_group_name = module.vpc.db_subnet_group\n}\n\n'
            'resource "aws_elasticache_cluster" "sessions" {\n  engine = "redis"\n'
            '  subnet_group_name = module.vpc.cache_subnet_group\n}\n\n'
            'resource "aws_s3_bucket" "assets" {\n  bucket = "acme-shop-assets"\n}\n\n'
            'resource "aws_cloudfront_distribution" "cdn" {\n'
            '  origin { domain_name = aws_s3_bucket.assets.bucket_regional_domain_name }\n}\n\n'
            'resource "aws_sqs_queue" "orders" {\n  name = "orders-placed"\n}\n'
        ),
        "variables.tf": 'variable "region" {\n  default = "eu-west-1"\n}\n',
        "outputs.tf": 'output "cluster" {\n  value = aws_eks_cluster.shop.name\n}\n',
        "modules/vpc/main.tf": 'resource "aws_vpc" "this" {\n  cidr_block = var.cidr\n}\n'
                               'resource "aws_subnet" "private" {\n  vpc_id = aws_vpc.this.id\n}\n',
        "modules/vpc/variables.tf": 'variable "cidr" {}\n',
        ".github/workflows/plan.yml": "on: pull_request\njobs:\n  plan:\n    runs-on: ubuntu-latest\n"
                                      "    steps:\n      - run: terraform fmt -check\n"
                                      "      - run: terraform validate\n",
    },
    "github.com/acme/service-config": {
        "README.md": "# service-config\nRuntime configuration and feature flags per environment.\n",
        "envs/dev/checkout-api.yaml": "replicas: 1\nlog_level: debug\n"
                                      "database_url: postgres://checkout@db.dev.internal/checkout\n",
        "envs/staging/checkout-api.yaml": "replicas: 2\nlog_level: info\n"
                                          "database_url: postgres://checkout@db.staging.internal/checkout\n",
        "envs/prod/checkout-api.yaml": "replicas: 6\nlog_level: warn\n"
                                       "database_url: postgres://checkout@db.prod.internal/checkout\n"
                                       "stripe_api_key: prod-plaintext-value-123\n",
        "envs/prod/flags.json": json.dumps({"one_click_checkout": False, "saved_cards": True}),
        "envs/dev/flags.json": json.dumps({"one_click_checkout": True, "saved_cards": True}),
        "CODEOWNERS": "* @acme/platform\n",
    },
}


def write(root: Path, files: dict[str, str]) -> None:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)


def main(out: Path) -> None:
    results = []
    for ref, files in REPOS.items():
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ref.rsplit("/", 1)[-1]
            write(root, files)
            a = analyze_path(root, ref)
        a["repo"] = ref
        drafts = {}
        for c in a["readiness"]["checks"]:
            gen = c.get("generate")
            if c["status"] == "fail" and gen and gen not in drafts:
                built = build(gen, a)
                if built:
                    drafts[gen] = {"title": built[0], "content": built[1], "data": built[2]}
        spec = a["api"].get("openapi")
        results.append({"analysis": a, "drafts": drafts, "conventions": conventions(a),
                        "openapi_yaml": yaml.safe_dump(spec, sort_keys=False) if spec else None})
        print(f"{ref}: {a['profile']['kind_names']} grade {a['readiness']['grade']} "
              f"api={len(a['api']['endpoints'])} pages={len(a['ui']['pages'])} "
              f"nodes={len(a['components']['nodes'])} drafts={list(drafts)}")
    out.write_text(json.dumps(results, indent=1))
    print(f"wrote {out} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    default = Path(__file__).resolve().parents[2] / "frontend" / "src" / "demo-repos.json"
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else default)
