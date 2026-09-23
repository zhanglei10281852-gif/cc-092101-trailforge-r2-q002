from __future__ import annotations

from datetime import UTC, datetime, timedelta


def _make_user(client, email: str, name: str) -> int:
    response = client.post(
        "/api/v1/users",
        json={"email": email, "display_name": name, "timezone": "Asia/Shanghai"},
    )
    assert response.status_code == 201, response.text
    user_id = response.json()["id"]
    profile = client.put(
        f"/api/v1/users/{user_id}/sport-profile",
        params={"actor_id": user_id},
        json={
            "height_cm": 172,
            "weight_kg": 68,
            "fitness_level": "intermediate",
            "outdoor_experience": "Weekend hiking",
            "weekly_training_minutes": 240,
        },
    )
    assert profile.status_code == 200, profile.text
    return user_id


def _make_route(client, organizer_id: int) -> int:
    response = client.post(
        "/api/v1/routes",
        params={"actor_id": organizer_id},
        json={
            "name": "Eligibility Ridge",
            "region": "Test",
            "distance_km": 10,
            "elevation_gain_m": 500,
            "elevation_loss_m": 500,
            "min_altitude_m": 100,
            "max_altitude_m": 600,
            "estimated_duration_minutes": 240,
            "difficulty": "moderate",
            "is_loop": True,
            "is_published": True,
            "segments": [
                {
                    "sequence": 1,
                    "name": "Main",
                    "distance_km": 10,
                    "elevation_gain_m": 500,
                    "estimated_duration_minutes": 240,
                    "difficulty": "moderate",
                    "start_latitude": 30,
                    "start_longitude": 120,
                    "end_latitude": 30,
                    "end_longitude": 120,
                }
            ],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _make_expedition(client, organizer_id: int, route_id: int) -> int:
    start = datetime.now(UTC) + timedelta(days=10)
    response = client.post(
        "/api/v1/expeditions",
        json={
            "organizer_id": organizer_id,
            "route_id": route_id,
            "name": "Policy Expedition",
            "meeting_location": "Trailhead",
            "meeting_at": (start - timedelta(hours=1)).isoformat(),
            "start_at": start.isoformat(),
            "end_at": (start + timedelta(hours=6)).isoformat(),
            "registration_deadline": (start - timedelta(days=1)).isoformat(),
            "capacity": 3,
            "minimum_fitness_level": 1,
            "risk_level": "high",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_policy_precheck_register_pending_review_chain(client) -> None:
    organizer = _make_user(client, "org@example.com", "Organizer")
    route_id = _make_route(client, organizer)
    expedition_id = _make_expedition(client, organizer, route_id)

    # Policy maintenance: require a contact and route asthma through review.
    policy_response = client.put(
        f"/api/v1/expeditions/{expedition_id}/eligibility-policy",
        params={"actor_id": organizer},
        json={
            "rules": {
                "training_requirements": [],
                "require_emergency_contact": True,
                "health_restriction_rules": [
                    {"restriction_name": "asthma", "action": "review"}
                ],
            },
            "change_note": "High risk event",
        },
    )
    assert policy_response.status_code == 200, policy_response.text
    policy = policy_response.json()
    assert policy["version"] == 1
    assert policy["is_active"] is True

    # Non-organizer cannot change the policy.
    intruder = _make_user(client, "intruder@example.com", "Intruder")
    forbidden = client.put(
        f"/api/v1/expeditions/{expedition_id}/eligibility-policy",
        params={"actor_id": intruder},
        json={"rules": {"require_emergency_contact": False}},
    )
    assert forbidden.status_code == 403

    applicant = _make_user(client, "asthma@example.com", "Asthmatic")
    # Precheck before adding a contact: rejected, and nothing is persisted.
    precheck = client.post(
        f"/api/v1/expeditions/{expedition_id}/eligibility-precheck",
        json={"user_id": applicant},
    )
    assert precheck.status_code == 200
    assert precheck.json()["outcome"] == "rejected"

    client.post(
        f"/api/v1/users/{applicant}/emergency-contacts",
        params={"actor_id": applicant},
        json={
            "name": "Contact",
            "relationship_label": "Family",
            "phone": "13800000000",
            "priority": 1,
        },
    )
    client.post(
        f"/api/v1/users/{applicant}/health-restrictions",
        params={"actor_id": applicant},
        json={"name": "Asthma", "severity": 2},
    )

    # Open registration and apply: only the review rule remains unsatisfied.
    client.post(
        f"/api/v1/expeditions/{expedition_id}/status",
        json={"target_status": "open", "actor_id": organizer, "reason": "go"},
    )
    register = client.post(
        f"/api/v1/expeditions/{expedition_id}/registrations",
        json={"user_id": applicant, "idempotency_key": "http-register-key-1"},
    )
    assert register.status_code == 201, register.text
    registration = register.json()
    assert registration["status"] == "pending"
    decision_id = registration["latest_decision_id"]
    assert decision_id is not None

    # The applicant cannot self-review.
    self_review = client.post(
        f"/api/v1/eligibility-decisions/{decision_id}/reviews",
        params={"actor_id": applicant},
        json={"decision": "approve", "reason": "self", "expected_version": 1},
    )
    assert self_review.status_code == 403

    # Organizer review with a missing/blank reason is rejected at validation.
    blank = client.post(
        f"/api/v1/eligibility-decisions/{decision_id}/reviews",
        params={"actor_id": organizer},
        json={"decision": "approve", "reason": "   ", "expected_version": 1},
    )
    assert blank.status_code == 422

    # Stale optimistic-lock version is refused.
    stale = client.post(
        f"/api/v1/eligibility-decisions/{decision_id}/reviews",
        params={"actor_id": organizer},
        json={"decision": "approve", "reason": "ok", "expected_version": 99},
    )
    assert stale.status_code == 409

    approved = client.post(
        f"/api/v1/eligibility-decisions/{decision_id}/reviews",
        params={"actor_id": organizer},
        json={
            "decision": "approve",
            "reason": "Medical clearance verified",
            "expected_version": 1,
        },
    )
    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert body["review_decision"] == "approve"
    assert body["review_facts"]["resulting_status"] == "confirmed"

    # Re-reviewing the same decision is rejected.
    duplicate = client.post(
        f"/api/v1/eligibility-decisions/{decision_id}/reviews",
        params={"actor_id": organizer},
        json={"decision": "reject", "reason": "again", "expected_version": 2},
    )
    assert duplicate.status_code == 409


def test_policy_versions_and_decision_listing(client) -> None:
    organizer = _make_user(client, "org2@example.com", "Organizer2")
    route_id = _make_route(client, organizer)
    expedition_id = _make_expedition(client, organizer, route_id)

    first = client.put(
        f"/api/v1/expeditions/{expedition_id}/eligibility-policy",
        params={"actor_id": organizer},
        json={"rules": {"require_emergency_contact": True}},
    )
    assert first.json()["version"] == 1
    second = client.put(
        f"/api/v1/expeditions/{expedition_id}/eligibility-policy",
        params={"actor_id": organizer},
        json={
            "rules": {"minimum_outdoor_level": 2},
            "expected_version": 1,
        },
    )
    assert second.status_code == 200
    assert second.json()["version"] == 2

    versions = client.get(
        f"/api/v1/expeditions/{expedition_id}/eligibility-policy/versions"
    ).json()
    assert [item["version"] for item in versions] == [1, 2]
    assert versions[0]["is_active"] is False
    assert versions[1]["is_active"] is True

    active = client.get(f"/api/v1/expeditions/{expedition_id}/eligibility-policy")
    assert active.json()["version"] == 2

    decisions = client.get(
        f"/api/v1/expeditions/{expedition_id}/eligibility-decisions",
        params={"actor_id": organizer},
    )
    assert decisions.status_code == 200
    assert decisions.json() == []


def test_legacy_expedition_without_policy_has_no_policy_endpoint(client) -> None:
    organizer = _make_user(client, "org3@example.com", "Organizer3")
    route_id = _make_route(client, organizer)
    expedition_id = _make_expedition(client, organizer, route_id)
    response = client.get(f"/api/v1/expeditions/{expedition_id}/eligibility-policy")
    assert response.status_code == 404
    versions = client.get(
        f"/api/v1/expeditions/{expedition_id}/eligibility-policy/versions"
    )
    assert versions.json() == []
