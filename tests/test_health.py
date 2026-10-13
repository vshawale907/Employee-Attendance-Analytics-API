"""
tests/test_health.py
Tests for GET /health endpoint.
"""
def test_health_check(test_client):
    response = test_client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
