"""Component view: what a repo is made of and what it connects to, with evidence.

Signals, each recorded with file and line:
- dependencies (database drivers, caches, queues, cloud SDKs, SaaS clients, observability)
- environment variables the code reads (DATABASE_URL, PAYMENTS_SERVICE_URL, ...)
- hosts in URLs in code and config (api.stripe.com, orders.svc.cluster.local, ...)
- docker-compose services and depends_on
- Kubernetes workloads and the services they call
- Terraform resources and the references between them
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

import yaml

from filament.readiness.scanner import Repo
from filament.repos.manifests import Dependency

# library -> (node id, label, category)
LIBS: dict[str, tuple[str, str, str]] = {}


def _lib(names: str, node: str, label: str, category: str) -> None:
    for n in names.split():
        LIBS[n] = (node, label, category)


_lib("psycopg psycopg2 psycopg2-binary asyncpg pg pg-promise postgres github.com/lib/pq "
     "github.com/jackc/pgx/v5 postgresql npgsql", "postgres", "PostgreSQL", "datastore")
_lib("mysqlclient pymysql mysql mysql2 github.com/go-sql-driver/mysql mysql-connector-java",
     "mysql", "MySQL", "datastore")
_lib("pymongo motor mongodb mongoose go.mongodb.org/mongo-driver spring-boot-starter-data-mongodb",
     "mongodb", "MongoDB", "datastore")
_lib("redis ioredis aioredis github.com/redis/go-redis/v9 github.com/go-redis/redis/v8 "
     "spring-boot-starter-data-redis jedis lettuce-core", "redis", "Redis", "cache")
_lib("memcache pymemcache memjs", "memcached", "Memcached", "cache")
_lib("elasticsearch @elastic/elasticsearch opensearch-py @opensearch-project/opensearch",
     "search", "Elasticsearch / OpenSearch", "datastore")
_lib("kafka-python confluent-kafka aiokafka kafkajs github.com/segmentio/kafka-go "
     "github.com/confluentinc/confluent-kafka-go spring-kafka kafka-clients", "kafka", "Kafka",
     "queue")
_lib("pika aio-pika amqplib amqp-connection-manager github.com/rabbitmq/amqp091-go "
     "spring-boot-starter-amqp", "rabbitmq", "RabbitMQ", "queue")
_lib("celery bullmq bull rq dramatiq", "workers", "Background workers", "queue")
_lib("nats nats.py github.com/nats-io/nats.go", "nats", "NATS", "queue")
_lib("boto3 aiobotocore aws-sdk @aws-sdk/client-s3 github.com/aws/aws-sdk-go-v2", "aws", "AWS",
     "cloud")
_lib("@aws-sdk/client-sqs", "sqs", "Amazon SQS", "queue")
_lib("@aws-sdk/client-dynamodb", "dynamodb", "DynamoDB", "datastore")
_lib("google-cloud-storage @google-cloud/storage cloud.google.com/go/storage", "gcs",
     "Google Cloud Storage", "cloud")
_lib("google-cloud-pubsub @google-cloud/pubsub cloud.google.com/go/pubsub", "pubsub",
     "Google Pub/Sub", "queue")
_lib("azure-storage-blob @azure/storage-blob", "azure-blob", "Azure Blob Storage", "cloud")
_lib("stripe @stripe/stripe-js github.com/stripe/stripe-go/v76", "stripe", "Stripe", "external")
_lib("twilio", "twilio", "Twilio", "external")
_lib("sendgrid @sendgrid/mail", "sendgrid", "SendGrid", "external")
_lib("anthropic @anthropic-ai/sdk", "anthropic", "Anthropic API", "external")
_lib("openai", "openai", "OpenAI API", "external")
_lib("slack-sdk @slack/web-api @slack/bolt", "slack", "Slack", "external")
_lib("auth0 @auth0/auth0-react @auth0/nextjs-auth0 okta @okta/okta-auth-js", "idp",
     "Identity provider", "external")
_lib("sentry-sdk @sentry/node @sentry/react @sentry/browser", "sentry", "Sentry",
     "observability")
_lib("ddtrace dd-trace datadog", "datadog", "Datadog", "observability")
_lib("opentelemetry-api opentelemetry-sdk @opentelemetry/api @opentelemetry/sdk-node",
     "otel", "OpenTelemetry collector", "observability")
_lib("prometheus-client prom-client", "prometheus", "Prometheus", "observability")
_lib("sqlalchemy prisma @prisma/client typeorm sequelize gorm.io/gorm", "database", "Database (ORM)",
     "datastore")
_lib("mcp @modelcontextprotocol/sdk", "mcp", "MCP servers", "external")

ENV_KEYWORDS = [("postgres", "postgres", "PostgreSQL", "datastore"),
                ("pg_", "postgres", "PostgreSQL", "datastore"),
                ("mysql", "mysql", "MySQL", "datastore"),
                ("mongo", "mongodb", "MongoDB", "datastore"),
                ("redis", "redis", "Redis", "cache"),
                ("kafka", "kafka", "Kafka", "queue"), ("rabbit", "rabbitmq", "RabbitMQ", "queue"),
                ("amqp", "rabbitmq", "RabbitMQ", "queue"), ("sqs", "sqs", "Amazon SQS", "queue"),
                ("s3", "s3", "Amazon S3", "cloud"), ("elastic", "search", "Elasticsearch", "datastore"),
                ("database", "database", "Database", "datastore"), ("db_", "database", "Database",
                                                                     "datastore"),
                ("stripe", "stripe", "Stripe", "external"), ("sentry", "sentry", "Sentry",
                                                             "observability"),
                ("otel", "otel", "OpenTelemetry collector", "observability"),
                ("anthropic", "anthropic", "Anthropic API", "external")]
ENV_SUFFIX = re.compile(r"^(\w+?)_(URL|URI|HOST|DSN|ENDPOINT|ADDR|ADDRESS|BROKERS?|BASE_URL|API_URL)$")
ENV_READ = re.compile(r"""(?:os\.environ(?:\.get)?\(?\[?|os\.getenv\(|process\.env\.|process\.env\[|"""
                      r"""System\.getenv\(|os\.Getenv\(|env::var\(|ENV\[)\s*['"]?([A-Z][A-Z0-9_]{2,})""")
DSN_RE = re.compile(r"\b(postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|rediss|amqps?|kafka|"
                    r"nats|clickhouse)://(?:[^@\s/'\"]+@)?([a-z0-9][a-z0-9.-]*)", re.I)
DSN_KIND = {"postgres": ("postgres", "PostgreSQL", "datastore"), "mysql": ("mysql", "MySQL", "datastore"),
            "mariadb": ("mysql", "MySQL", "datastore"), "mongodb": ("mongodb", "MongoDB", "datastore"),
            "redis": ("redis", "Redis", "cache"), "amqp": ("rabbitmq", "RabbitMQ", "queue"),
            "kafka": ("kafka", "Kafka", "queue"), "nats": ("nats", "NATS", "queue"),
            "clickhouse": ("clickhouse", "ClickHouse", "datastore")}
URL_RE = re.compile(r"https?://([a-z0-9][a-z0-9.-]*\.[a-z]{2,}|[a-z][a-z0-9-]*(?:\.svc(?:\.cluster\.local)?)?)"
                    r"(?::\d+)?", re.I)
IGNORE_HOSTS = re.compile(r"localhost|127\.0\.0\.1|0\.0\.0\.0|example\.(com|org)|w3\.org|schema|"
                          r"json-schema|github\.com|githubusercontent|npmjs|pypi\.org|python\.org|"
                          r"mozilla|apache\.org|opensource|creativecommons|googleapis\.com/css|"
                          r"fonts\.|cdn\.|unpkg|jsdelivr|swagger\.io|openapis\.org|readthedocs|"
                          r"docs\.|wikipedia|claude\.ai|anthropic\.com/news|shields\.io|^www\.")
KNOWN_HOSTS = [("stripe.com", "stripe", "Stripe", "external"),
               ("slack.com", "slack", "Slack", "external"),
               ("amazonaws.com", "aws", "AWS", "cloud"),
               ("api.anthropic.com", "anthropic", "Anthropic API", "external"),
               ("api.openai.com", "openai", "OpenAI API", "external"),
               ("sentry.io", "sentry", "Sentry", "observability"),
               ("datadoghq", "datadog", "Datadog", "observability"),
               ("twilio.com", "twilio", "Twilio", "external"),
               ("api.github.com", "github-api", "GitHub API", "external")]
IMAGE_HINTS = [("postgres", "datastore", "PostgreSQL"), ("pgvector", "datastore", "PostgreSQL"),
               ("mysql", "datastore", "MySQL"), ("mariadb", "datastore", "MariaDB"),
               ("mongo", "datastore", "MongoDB"), ("redis", "cache", "Redis"),
               ("memcached", "cache", "Memcached"), ("kafka", "queue", "Kafka"),
               ("rabbitmq", "queue", "RabbitMQ"), ("elasticsearch", "datastore", "Elasticsearch"),
               ("opensearch", "datastore", "OpenSearch"), ("localstack", "cloud", "LocalStack"),
               ("minio", "cloud", "MinIO"), ("nginx", "service", "nginx"),
               ("prometheus", "observability", "Prometheus"), ("grafana", "observability", "Grafana"),
               ("jaeger", "observability", "Jaeger"), ("otel", "observability", "OTel collector")]
TF_TYPES = [("db_instance", "datastore"), ("rds", "datastore"), ("dynamodb", "datastore"),
            ("sql_database", "datastore"), ("cosmosdb", "datastore"), ("elasticache", "cache"),
            ("redis", "cache"), ("sqs", "queue"), ("sns", "queue"), ("kinesis", "queue"),
            ("pubsub", "queue"), ("msk", "queue"), ("eventhub", "queue"), ("s3_bucket", "cloud"),
            ("storage_bucket", "cloud"), ("storage_account", "cloud"), ("lambda", "compute"),
            ("ecs_", "compute"), ("eks", "compute"), ("container_cluster", "compute"),
            ("kubernetes_cluster", "compute"), ("instance", "compute"), ("cloud_run", "compute"),
            ("lb", "network"), ("vpc", "network"), ("subnet", "network"), ("security_group", "network"),
            ("route53", "network"), ("cloudfront", "network"), ("iam", "security"),
            ("kms", "security"), ("secret", "security"), ("cloudwatch", "observability")]

TEXT_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".java", ".kt", ".rs", ".rb", ".php",
            ".cs", ".yaml", ".yml", ".json", ".toml", ".env", ".properties", ".tf", ".conf", ".ini"}
SKIP_PARTS = {"tests", "test", "__tests__", "fixtures", "docs", "examples", "mocks"}


class Graph:
    def __init__(self, center: str):
        self.center = center
        self.nodes: dict[str, dict] = {center: {"id": center, "label": center, "category": "self",
                                                "evidence": []}}
        self.edges: dict[tuple[str, str], str] = {}

    def node(self, nid: str, label: str, category: str, file: str, line: int | None,
             detail: str) -> str:
        nid = re.sub(r"[^a-zA-Z0-9_-]", "-", nid.lower())[:60] or "node"
        n = self.nodes.setdefault(nid, {"id": nid, "label": label, "category": category,
                                        "evidence": []})
        if n["category"] in {"service", "unknown"} and category not in {"service", "unknown"}:
            n["category"], n["label"] = category, label
        ev = {"file": file, "line": line, "detail": detail}
        if ev not in n["evidence"] and len(n["evidence"]) < 8:
            n["evidence"].append(ev)
        return nid

    def edge(self, a: str, b: str, label: str = "") -> None:
        if a != b and a in self.nodes and b in self.nodes:
            self.edges.setdefault((a, b), label)

    def to_dict(self) -> dict:
        nodes = list(self.nodes.values())
        edges = [{"from": a, "to": b, "label": lbl} for (a, b), lbl in self.edges.items()]
        return {"nodes": nodes, "edges": edges, "mermaid": mermaid(nodes, edges, self.center)}


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _files(repo: Repo) -> list[str]:
    return [str(p) for p in repo.files if p.suffix.lower() in TEXT_EXT
            and not (set(p.parts[:-1]) & SKIP_PARTS) and p.name not in {"package-lock.json",
                                                                         "openapi.json"}]


def _from_deps(g: Graph, deps: list[Dependency]) -> None:
    for d in deps:
        if d.dev:
            continue
        hit = LIBS.get(d.name)
        if hit:
            nid = g.node(hit[0], hit[1], hit[2], d.file, None, f"depends on {d.name}")
            g.edge(g.center, nid, "uses")


def _from_env(g: Graph, repo: Repo, texts: dict[str, str]) -> None:
    for f, text in texts.items():
        for m in ENV_READ.finditer(text):
            var = m.group(1)
            sm = ENV_SUFFIX.match(var)
            if not sm:
                continue
            prefix = re.sub(r"^(react_app|vite|next_public|nuxt_public|expo_public|vue_app)_", "",
                            sm.group(1).lower())
            if sm.group(2) == "API_URL" and not prefix.endswith("api"):
                prefix += "_api"
            known = next((k for k in ENV_KEYWORDS if k[0] in prefix + "_"), None)
            if known:
                nid = g.node(known[1], known[2], known[3], f, _line(text, m.start()), f"reads {var}")
            else:
                name = prefix.replace("_", "-")
                nid = g.node(name, name, "service", f, _line(text, m.start()), f"reads {var}")
            g.edge(g.center, nid, "calls" if not known else "uses")


def _from_urls(g: Graph, texts: dict[str, str]) -> None:
    for f, text in texts.items():
        if f.endswith((".json",)) and "package" in f:
            continue
        for m in URL_RE.finditer(text):
            host = m.group(1).lower()
            if IGNORE_HOSTS.search(host):
                continue
            known = next((k for k in KNOWN_HOSTS if k[0] in host), None)
            line = _line(text, m.start())
            if known:
                nid = g.node(known[1], known[2], known[3], f, line, f"calls {host}")
            elif ".svc" in host or "." not in host or host.endswith((".internal", ".local")):
                name = host.split(".")[0]
                nid = g.node(name, name, "service", f, line, f"calls {host}")
            else:
                nid = g.node(host, host, "external", f, line, f"calls {host}")
            g.edge(g.center, nid, "calls")


def _from_dsn(g: Graph, texts: dict[str, str]) -> None:
    """Connection strings (postgres://, redis://, ...) in code and config files."""
    for f, text in texts.items():
        for m in DSN_RE.finditer(text):
            scheme = re.sub(r"(ql|\+srv|s)$", "", m.group(1).lower())
            kind = DSN_KIND.get(scheme) or DSN_KIND.get(m.group(1).lower())
            if not kind:
                continue
            host = m.group(2).lower()
            env = next((e for e in ("prod", "staging", "dev", "test") if f"/{e}/" in f"/{f}"), None)
            nid = g.node(kind[0], kind[1], kind[2], f, _line(text, m.start()),
                         f"connects to {host}" + (f" ({env})" if env else ""))
            g.edge(g.center, nid, "uses")


def _from_compose(g: Graph, repo: Repo) -> None:
    for p in repo.files:
        if p.name not in {"docker-compose.yml", "docker-compose.yaml", "compose.yaml", "compose.yml"}:
            continue
        f = str(p)
        try:
            doc = yaml.safe_load(repo.read(f)) or {}
        except yaml.YAMLError:
            continue
        services = doc.get("services") or {}
        ids = {}
        for name, svc in services.items():
            svc = svc or {}
            image = str(svc.get("image", ""))
            hint = next((h for h in IMAGE_HINTS if h[0] in image.lower()), None)
            build = svc.get("build")
            ctx = build.get("context", ".") if isinstance(build, dict) else build
            if build is not None and str(ctx).rstrip("/") in {".", ""}:
                ids[name] = g.center  # this repo itself
                g.nodes[g.center]["evidence"].append(
                    {"file": f, "line": None, "detail": f"compose service '{name}'"})
            elif build is not None:
                ids[name] = g.node(name, name, "service", f, None, "built from this repo")
                g.edge(g.center, ids[name], "contains")
            elif hint:
                tech = next((k[1] for k in ENV_KEYWORDS if k[2] == hint[2]), None)
                nid = tech if tech and re.sub(r"[^a-z0-9_-]", "-", tech) in g.nodes else name
                ids[name] = g.node(nid, hint[2] if nid == tech else f"{hint[2]} ({name})", hint[1],
                                   f, None, f"compose service '{name}', image {image}")
            else:
                ids[name] = g.node(name, name, "service", f, None, f"image {image}" if image else "")
        for name, svc in services.items():
            dep = (svc or {}).get("depends_on") or []
            for target in (dep.keys() if isinstance(dep, dict) else dep):
                if target in ids:
                    g.edge(ids[name], ids[target], "depends on")


def _from_k8s(g: Graph, repo: Repo) -> None:
    for p in repo.files:
        if p.suffix not in {".yaml", ".yml"} or set(p.parts) & {"node_modules"}:
            continue
        f = str(p)
        text = repo.read(f, 200_000)
        if "apiVersion" not in text or "kind:" not in text:
            continue
        try:
            docs = list(yaml.safe_load_all(text))
        except yaml.YAMLError:
            continue
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            kind = doc.get("kind")
            name = (doc.get("metadata") or {}).get("name", "")
            if kind in {"Deployment", "StatefulSet", "DaemonSet", "CronJob", "Job"} and name:
                nid = g.node(name, f"{name} ({kind})", "compute", f, None, f"Kubernetes {kind}")
                g.edge(g.center, nid, "deploys")
                spec = doc.get("spec") or {}
                tpl = (spec.get("jobTemplate", {}).get("spec", {}) or spec).get("template", {})
                for c in (tpl.get("spec", {}) or {}).get("containers", []) or []:
                    for env in c.get("env", []) or []:
                        value = str(env.get("value", ""))
                        for m in URL_RE.finditer(value):
                            host = m.group(1).split(".")[0]
                            if not IGNORE_HOSTS.search(m.group(1)):
                                target = g.node(host, host, "service", f, None,
                                                f"{env.get('name')}={value}")
                                g.edge(nid, target, "calls")
            elif kind == "Ingress" and name:
                nid = g.node(f"ingress-{name}", f"Ingress {name}", "network", f, None, "Ingress")
                g.edge(nid, g.center, "routes to")


def _from_terraform(g: Graph, repo: Repo) -> None:
    resources: dict[str, str] = {}
    bodies: dict[str, str] = {}
    for p in repo.files:
        if p.suffix != ".tf":
            continue
        f = str(p)
        text = repo.read(f, 300_000)
        for m in re.finditer(r'(resource|module|data)\s+"([\w-]+)"\s*(?:"([\w-]+)")?\s*\{', text):
            block, rtype, rname = m.group(1), m.group(2), m.group(3)
            if block == "module":
                key, label, category = f"module.{rtype}", f"module {rtype}", "module"
            elif block == "data":
                continue
            else:
                key = f"{rtype}.{rname}"
                category = next((c for kw, c in TF_TYPES if kw in rtype), "resource")
                label = f"{rname} ({rtype})"
            resources[key] = g.node(key, label, category, f, _line(text, m.start()), block)
            g.edge(g.center, resources[key], "provisions")
            end = text.find("\n}", m.end())
            bodies[key] = text[m.end(): end if end > 0 else m.end() + 3000]
    for key, body in bodies.items():
        for other, nid in resources.items():
            if other != key and re.search(rf"\b{re.escape(other)}\b", body):
                g.edge(resources[key], nid, "references")


def mermaid(nodes: list[dict], edges: list[dict], center: str) -> str:
    def safe(s: str) -> str:
        return re.sub(r'["\[\]{}()<>|#;]', " ", s)[:48]

    lines = ["flowchart LR"]
    for n in nodes:
        nid = "n_" + re.sub(r"[^a-zA-Z0-9_]", "_", n["id"])
        shape = {"datastore": ('[("', '")]'), "cache": ('[("', '")]'),
                 "queue": ('[["', '"]]'), "self": ('(["', '"])')}.get(n["category"], ('["', '"]'))
        lines.append(f"  {nid}{shape[0]}{safe(n['label'])}{shape[1]}")
    for e in edges:
        a = "n_" + re.sub(r"[^a-zA-Z0-9_]", "_", e["from"])
        b = "n_" + re.sub(r"[^a-zA-Z0-9_]", "_", e["to"])
        lines.append(f"  {a} -->|{safe(e['label'])}| {b}" if e["label"] else f"  {a} --> {b}")
    classes = {"self": "fill:#fbf1df,stroke:#c47f17,stroke-width:2px",
               "datastore": "fill:#e7f1fb,stroke:#5b8cc4", "cache": "fill:#e7f1fb,stroke:#5b8cc4",
               "queue": "fill:#efedff,stroke:#7b6fd0", "external": "fill:#f9ece7,stroke:#c4825b",
               "cloud": "fill:#e5f4e4,stroke:#5ba35b", "observability": "fill:#f0efec,stroke:#999",
               "service": "fill:#fff,stroke:#666", "compute": "fill:#fff,stroke:#666"}
    for cat, style in classes.items():
        members = ["n_" + re.sub(r"[^a-zA-Z0-9_]", "_", n["id"]) for n in nodes
                   if n["category"] == cat]
        if members:
            lines.append(f"  classDef {cat} {style}")
            lines.append(f"  class {','.join(members)} {cat}")
    return "\n".join(lines)


def _merge_generic_database(g: Graph) -> None:
    """Fold the generic "Database (ORM)" node into the concrete database when one is known."""
    generic = g.nodes.get("database")
    concrete = [n for nid, n in g.nodes.items() if nid != "database" and n["category"] == "datastore"]
    if not generic or len(concrete) != 1:
        return
    target = concrete[0]
    target["evidence"] = generic["evidence"] + target["evidence"]
    del g.nodes["database"]
    g.edges = {(target["id"] if s == "database" else s, target["id"] if t == "database" else t): lbl
               for (s, t), lbl in g.edges.items() if not (s == t == "database")}
    g.edges.pop((target["id"], target["id"]), None)


def extract(repo: Repo, name: str, deps: list[Dependency]) -> dict:
    g = Graph(name)
    texts = {f: repo.read(f, 150_000) for f in _files(repo)[:3000]}
    _from_deps(g, deps)
    _from_env(g, repo, texts)
    _from_urls(g, {f: t for f, t in texts.items() if not f.endswith(".md")})
    _from_dsn(g, texts)
    _from_compose(g, repo)
    _from_k8s(g, repo)
    _from_terraform(g, repo)
    _merge_generic_database(g)
    out = g.to_dict()
    by_cat: dict[str, int] = {}
    for n in out["nodes"]:
        by_cat[n["category"]] = by_cat.get(n["category"], 0) + 1
    out["summary"] = by_cat
    out["name"] = PurePosixPath(name).name
    return out
