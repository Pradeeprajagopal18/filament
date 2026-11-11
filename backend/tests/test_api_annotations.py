"""API documentation from code: springdoc/springfox, swaggo, @nestjs/swagger, FastAPI; spec
generators and where they serve the spec; live specs from a running service."""

import json

from igniteai.repos.analyze import analyze_path
from tests.test_repos import write


def api_of(tmp_path, files, **kw):
    root = write(tmp_path / "svc", files)
    return analyze_path(root, str(root), code_graph=False, **kw)["api"]


def by_key(api):
    return {f"{e['method']} {e['path']}": e for e in api["endpoints"]}


def test_go_swaggo(tmp_path):
    api = api_of(tmp_path, {
        "go.mod": "module github.com/acme/orders\nrequire (\n github.com/gin-gonic/gin v1.10.0\n"
                  " github.com/swaggo/swag v1.16.3\n)\n",
        "main.go": "package main\n// @title Orders API\n// @version 1.0\n// @BasePath /api/v1\n"
                   "func main() {\n r := gin.Default()\n v1 := r.Group(\"/api/v1\")\n"
                   " v1.GET(\"/orders/:id\", handlers.GetOrder)\n}\n",
        "model/order.go": "package model\ntype Order struct {\n  ID int64 `json:\"id\"`\n"
                          "  Total float64 `json:\"total_cents\" binding:\"required\"`\n"
                          "  Items []Item `json:\"items\"`\n  secret string\n}\n"
                          "type Item struct {\n  SKU string `json:\"sku\"`\n}\n"
                          "type Unused struct { X int }\n",
        "handlers/orders.go": (
            "package handlers\n"
            "// GetOrder godoc\n// @Summary      Get an order\n"
            "// @Description  Returns one order with its items\n// @Tags         orders\n"
            "// @Param        id   path  int  true  \"Order ID\"\n"
            "// @Success      200  {object}  model.Order\n"
            "// @Failure      404  {object}  httputil.HTTPError  \"order not found\"\n"
            "// @Security     ApiKeyAuth\n// @Router       /orders/{id} [get]\n"
            "func GetOrder(c *gin.Context) {}\n\n"
            "// @Summary  Order stats\n// @Tags admin\n// @Success 200 {array} model.Order\n"
            "// @Router   /admin/stats [get]\nfunc Stats(c *gin.Context) {}\n"),
    })
    eps = by_key(api)
    assert set(eps) == {"GET /api/v1/orders/{id}", "GET /api/v1/admin/stats"}  # merged, no dupes
    get = eps["GET /api/v1/orders/{id}"]
    assert get["summary"] == "Get an order" and get["tag"] == "orders" and get["auth"] == "bearer"
    assert get["documented"] == "swaggo" and get["handler"] == "handlers.GetOrder"
    assert get["params"][0]["description"] == "Order ID"
    assert get["params"][0]["schema"] == {"type": "integer"}
    assert get["responses"]["200"]["schema"] == {"$ref": "#/components/schemas/Order"}
    assert get["responses"]["404"]["description"] == "order not found"
    stats = eps["GET /api/v1/admin/stats"]["responses"]["200"]["schema"]
    assert stats == {"type": "array", "items": {"$ref": "#/components/schemas/Order"}}
    order = api["schemas"]["Order"]
    assert set(order["properties"]) == {"id", "total_cents", "items"}
    assert order["required"] == ["total_cents"]
    assert order["properties"]["items"]["items"] == {"$ref": "#/components/schemas/Item"}
    assert "Unused" not in api["schemas"]  # only models the endpoints use
    assert api["generators"][0]["generator"] == "swaggo"
    assert api["openapi"]["paths"]["/api/v1/orders/{id}"]["get"]["summary"] == "Get an order"


def test_springdoc_and_springfox(tmp_path):
    api = api_of(tmp_path, {
        "pom.xml": "<project><dependencies><dependency><artifactId>spring-boot-starter-web"
                   "</artifactId></dependency><dependency><artifactId>"
                   "springdoc-openapi-starter-webmvc-ui</artifactId></dependency></dependencies>"
                   "</project>",
        "src/main/resources/application.yml":
            "springdoc:\n  api-docs:\n    path: /docs/openapi\n",
        "src/main/java/acme/OwnerController.java": (
            "package acme;\n@RestController\n@RequestMapping(\"/owners\")\n"
            "@Tag(name = \"Owners\", description = \"Pet owners\")\n"
            "public class OwnerController {\n"
            "  @Operation(summary = \"Find an owner\", description = \"By id, with pets\")\n"
            "  @ApiResponse(responseCode = \"404\", description = \"No such owner\")\n"
            "  @GetMapping(\"/{id}\")\n"
            "  public Owner find(@Parameter(description = \"Owner id\") @PathVariable int id) {\n"
            "    return null;\n  }\n\n"
            "  @Hidden\n  @DeleteMapping(\"/{id}/purge\")\n"
            "  public void purge(@PathVariable int id) {}\n"
            "}\n"),
        "src/main/java/acme/LegacyController.java": (
            "package acme;\n@RestController\n@Api(tags = \"legacy\")\n"
            "public class LegacyController {\n"
            "  @ApiOperation(value = \"List visits\", notes = \"Oldest first\")\n"
            "  @ApiResponses({@ApiResponse(code = 403, message = \"Not your visits\")})\n"
            "  @GetMapping(\"/visits\")\n  public List<Visit> visits() { return null; }\n}\n"),
    })
    eps = by_key(api)
    assert "DELETE /owners/{id}/purge" not in eps  # @Hidden
    find = eps["GET /owners/{id}"]
    assert (find["summary"], find["description"], find["tag"]) == \
        ("Find an owner", "By id, with pets", "Owners")
    assert find["responses"]["404"]["description"] == "No such owner"
    assert find["params"][0]["description"] == "Owner id" and find["documented"] == "springdoc"
    visits = eps["GET /visits"]
    assert (visits["summary"], visits["description"], visits["tag"]) == \
        ("List visits", "Oldest first", "legacy")
    assert visits["responses"]["403"]["description"] == "Not your visits"
    assert api["generators"] == [{"generator": "springdoc", "spec_path": "/docs/openapi",
                                  "evidence": "springdoc-openapi-starter-webmvc-ui"}]


def test_nestjs_swagger(tmp_path):
    api = api_of(tmp_path, {
        "package.json": json.dumps({"dependencies": {"@nestjs/core": "10",
                                                     "@nestjs/swagger": "7"}}),
        "src/main.ts": "SwaggerModule.setup('docs', app, document)\n",
        "src/orders.controller.ts": (
            "@ApiTags('orders')\n@ApiBearerAuth()\n@Controller('orders')\n"
            "export class OrdersController {\n"
            "  @Get(':id')\n  @ApiOperation({ summary: 'Get an order' })\n"
            "  @ApiNotFoundResponse({ description: 'Order not found' })\n"
            "  findOne(@Param('id') id: string) { return id }\n\n"
            "  @Post('reindex')\n  @ApiExcludeEndpoint()\n  reindex() {}\n}\n"),
    })
    eps = by_key(api)
    assert set(eps) == {"GET /orders/{id}"}
    e = eps["GET /orders/{id}"]
    assert (e["summary"], e["tag"], e["auth"]) == ("Get an order", "orders", "bearer")
    assert e["responses"]["404"]["description"] == "Order not found"
    assert api["generators"][0] == {"generator": "nestjs-swagger", "spec_path": "/docs-json",
                                    "evidence": "@nestjs/swagger"}


def test_fastapi_docs(tmp_path):
    api = api_of(tmp_path, {
        "requirements.txt": "fastapi\n",
        "app/main.py": (
            "from fastapi import FastAPI\napp = FastAPI(openapi_url='/api/openapi.json')\n\n"
            "@app.get('/carts/{cart_id}', tags=['carts'], responses={404: {'description': "
            "'Cart not found'}})\ndef get_cart(cart_id: int):\n"
            "    \"\"\"Fetch a cart.\n\n    Includes line items and totals.\n    \"\"\"\n"
            "    return {}\n\n"
            "@app.get('/internal/health', include_in_schema=False)\ndef health():\n    return 1\n"),
    })
    eps = by_key(api)
    assert set(eps) == {"GET /carts/{cart_id}"}
    e = eps["GET /carts/{cart_id}"]
    assert (e["summary"], e["description"], e["tag"]) == \
        ("Fetch a cart.", "Includes line items and totals.", "carts")
    assert e["responses"]["404"]["description"] == "Cart not found"
    assert api["generators"][0]["spec_path"] == "/api/openapi.json"


def test_live_spec_from_a_running_service(tmp_path, monkeypatch):
    files = {"requirements.txt": "fastapi\n",
             "app/main.py": "from fastapi import FastAPI\napp = FastAPI()\n"
                            "@app.get('/carts')\ndef carts():\n    return []\n"}
    doc = {"openapi": "3.1.0", "info": {"title": "x", "version": "1"},
           "paths": {"/carts": {"get": {"summary": "List carts"}},
                     "/carts/{id}": {"get": {"summary": "One cart"}}}}

    class R:
        status_code = 200
        headers = {"content-type": "application/json"}
        text = json.dumps(doc)

        def json(self):
            return doc

    monkeypatch.setattr("httpx.get", lambda url, **kw: R())
    api = api_of(tmp_path, files, spec_url="http://localhost:8000/openapi.json")
    assert api["source"] == "live" and api["specs"] == ["http://localhost:8000/openapi.json"]
    assert set(by_key(api)) == {"GET /carts", "GET /carts/{id}"}
    assert api["drift"] and api["live_error"] is None

    import httpx

    def down(url, **kw):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr("httpx.get", down)
    api = api_of(tmp_path, files, spec_url="http://localhost:8000/openapi.json")
    assert api["source"] == "inferred" and "Could not reach" in api["live_error"]


def test_spec_url_setting_and_rescans(client, ws, tmp_path, monkeypatch):
    root = write(tmp_path / "svc", {"requirements.txt": "fastapi\n", "app/main.py":
                                    "from fastapi import FastAPI\napp = FastAPI()\n"
                                    "@app.get('/carts')\ndef carts():\n    return []\n"})
    r = client.post(f"/api/workspaces/{ws['id']}/repos", json={"repo": str(root)}).json()
    assert r["analysis"]["api"]["generators"][0]["spec_path"] == "/openapi.json"
    assert r["spec_url"] is None
    doc = {"openapi": "3.1.0", "info": {"title": "x", "version": "1"},
           "paths": {"/carts": {"get": {}}, "/orders": {"post": {}}}}

    class R:
        status_code = 200
        headers = {"content-type": "application/json"}
        text = json.dumps(doc)

        def json(self):
            return doc

    monkeypatch.setattr("httpx.get", lambda url, **kw: R())
    bad = client.patch(f"/api/repos/{r['id']}", json={"spec_url": "localhost:8000"})
    assert bad.status_code == 422
    client.patch(f"/api/repos/{r['id']}", json={"spec_url": "http://localhost:8000/openapi.json"})
    r2 = client.post(f"/api/repos/{r['id']}/rescan").json()
    assert r2["spec_url"] == "http://localhost:8000/openapi.json"
    assert r2["analysis"]["api"]["source"] == "live"
    assert len(r2["analysis"]["api"]["endpoints"]) == 2
    client.patch(f"/api/repos/{r['id']}", json={"spec_url": ""})
    r3 = client.post(f"/api/repos/{r['id']}/rescan").json()
    assert r3["spec_url"] is None and r3["analysis"]["api"]["source"] == "inferred"
