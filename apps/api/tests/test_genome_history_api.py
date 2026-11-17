import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from gitwise_api import main
from gitwise_api.auth import SESSION_COOKIE, AuthComponents
from gitwise_api.genome import SoftwareGenomeSnapshot
from gitwise_api.github_oauth import GitHubOAuthClient
from gitwise_api.github_user import GitHubUserClient
from gitwise_api.sessions import SessionStore
from gitwise_api.snapshot_identity import snapshot_storage_key
from gitwise_api.storage import GenomeSnapshotStore
from gitwise_api.token_crypto import TokenCipher
from sqlalchemy import text

DATABASE_URL = os.getenv("GITWISE_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="PostgreSQL integration database is not configured"
)
NOT_FOUND = {"detail": "Software Genome snapshot not found."}


class FakeGitHubApi:
    """api.github.com as seen by each user's GitHub App access token."""

    def __init__(self) -> None:
        self.members: dict[str, tuple[int, str, list[int]]] = {}
        self.down = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("GitHub is unreachable", request=request)
        token = request.headers["Authorization"].removeprefix("Bearer ")
        member = self.members.get(token)
        if member is None:
            return httpx.Response(401, json={"message": "Bad credentials"})
        user_id, login, installations = member
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": user_id, "login": login})
        page = int(request.url.params["page"])
        ids = [{"id": item} for item in installations] if page == 1 else []
        return httpx.Response(200, json={"installations": ids})


class World:
    """Three installations, three repositories and four people.

    installation A and B both grant repository 1 (overlap); C grants repository 2
    (disjoint from A and B); B alone grants repository 3.
    """

    def __init__(self) -> None:
        base = int.from_bytes(os.urandom(4), "big") + 1
        self.inst_a, self.inst_b, self.inst_c = base, base + 1, base + 2
        repo_base = int.from_bytes(os.urandom(5), "big") + 1
        self.repo1, self.repo2, self.repo3 = repo_base, repo_base + 1, repo_base + 2
        user_base = int.from_bytes(os.urandom(4), "big") + 1
        self.people = {
            "alice": (user_base, [self.inst_a, self.inst_b]),
            "bob": (user_base + 1, [self.inst_b]),
            "carol": (user_base + 2, [self.inst_c]),
            "dave": (user_base + 3, []),
        }
        self.github = FakeGitHubApi()
        self.sessions: SessionStore
        self.genome: GenomeSnapshotStore
        self.keys: dict[int, list[str]] = {self.repo1: [], self.repo2: [], self.repo3: []}

    def _snapshot(self, repository_id: int, commit: str) -> SoftwareGenomeSnapshot:
        return SoftwareGenomeSnapshot(
            repository=f"gitwise-integration/repo-{repository_id}",
            repository_url=f"https://github.com/gitwise-integration/repo-{repository_id}",
            provider_repository_id=repository_id,
            default_branch="main",
            commit_sha=commit * 40,
            tree_sha="f" * 40,
            is_private=True,
            generated_at=datetime.now(UTC),
        )

    async def setup(self) -> None:
        cipher = TokenCipher([Fernet.generate_key()])
        self.sessions = SessionStore.from_url(DATABASE_URL, cipher)
        self.genome = GenomeSnapshotStore.from_url(DATABASE_URL)
        grants = [
            (self.repo1, self.inst_a, "write"),
            (self.repo1, self.inst_b, "read"),
            (self.repo2, self.inst_c, "write"),
            (self.repo3, self.inst_b, "write"),
        ]
        async with self.genome._engine.begin() as connection:
            for repository_id in (self.repo1, self.repo2, self.repo3):
                await connection.execute(
                    text("""
                        INSERT INTO provider_repositories (
                            provider, provider_repository_id, owner_login,
                            repository_name, canonical_url, is_private
                        ) VALUES ('github', :id, 'gitwise-integration', :name, :url, true)
                    """),
                    {
                        "id": repository_id,
                        "name": f"repo-{repository_id}",
                        "url": f"https://github.com/gitwise-integration/repo-{repository_id}",
                    },
                )
            for repository_id, installation, permission in grants:
                await connection.execute(
                    text("""
                        INSERT INTO repository_access_grants (
                            grant_id, provider, provider_repository_id, principal_type,
                            principal_id, permission, granted_by
                        ) VALUES (
                            :grant_id, 'github', :id, 'github_app_installation',
                            :principal, :permission, 'integration-test'
                        )
                    """),
                    {
                        "grant_id": str(uuid4()),
                        "id": repository_id,
                        "principal": str(installation),
                        "permission": permission,
                    },
                )
        writers = {self.repo1: self.inst_a, self.repo2: self.inst_c, self.repo3: self.inst_b}
        for repository_id, commit in (
            (self.repo1, "1"),
            (self.repo1, "2"),
            (self.repo2, "3"),
            (self.repo3, "4"),
        ):
            snapshot = self._snapshot(repository_id, commit)
            await self.genome.store_snapshot(
                snapshot,
                principal_type="github_app_installation",
                principal_id=str(writers[repository_id]),
            )
            self.keys[repository_id].append(snapshot_storage_key(snapshot))

        main.app.state.genome_snapshot_store = self.genome
        main.app.state.auth_components = AuthComponents(
            sessions=self.sessions,
            oauth=GitHubOAuthClient(
                client_id="Iv1.test",
                client_secret="secret",
                redirect_uri="https://app.gitwise.test/auth/github/callback",
            ),
            public_base_url="https://app.gitwise.test",
            user_client_factory=lambda token: GitHubUserClient(
                token,
                client=httpx.AsyncClient(
                    base_url="https://api.github.com",
                    transport=httpx.MockTransport(self.github.handler),
                ),
            ),
        )

    async def browser_for(self, name: str) -> httpx.AsyncClient:
        user_id, installations = self.people[name]
        token = f"ghu_{name}_{uuid4().hex}"
        self.github.members[token] = (user_id, name, installations)
        issued = await self.sessions.create_session(
            github_user_id=user_id, github_login=name, access_token=token
        )
        browser = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app),
            base_url="https://gitwise.test",
        )
        browser.cookies.set(SESSION_COOKIE, issued.session_token)
        browser.token = token  # type: ignore[attr-defined]
        return browser

    async def revoke_grant(self, installation: int, repository_id: int) -> None:
        async with self.genome._engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE repository_access_grants SET revoked_at = now() "
                    "WHERE principal_id = :p AND provider_repository_id = :r"
                ),
                {"p": str(installation), "r": repository_id},
            )

    async def teardown(self) -> None:
        main.app.state.auth_components = None
        main.app.state.genome_snapshot_store = None
        await self.sessions.close()
        await self.genome.close()


def _run(scenario) -> None:
    async def exercise() -> None:
        world = World()
        await world.setup()
        try:
            await scenario(world)
        finally:
            await world.teardown()

    asyncio.run(exercise())


def test_overlapping_and_disjoint_installation_access() -> None:
    async def scenario(world: World) -> None:
        alice = await world.browser_for("alice")
        bob = await world.browser_for("bob")
        carol = await world.browser_for("carol")
        dave = await world.browser_for("dave")
        try:
            # Alice reaches repository 1 through two installations. That overlap must
            # return the snapshot once, not fail or duplicate it.
            overlap = await alice.get(f"/repositories/{world.repo1}/snapshots")
            assert overlap.status_code == 200
            listed = [item["snapshot_key"] for item in overlap.json()["snapshots"]]
            assert sorted(listed) == sorted(world.keys[world.repo1])
            assert len(listed) == len(set(listed)) == 2
            assert overlap.headers["cache-control"] == "private, no-store"

            for key in world.keys[world.repo1]:
                one = await alice.get(f"/snapshots/{key}")
                assert one.status_code == 200
                assert one.json()["provider_repository_id"] == world.repo1

            # Bob shares installation B: he sees repositories 1 and 3, never 2.
            assert (await bob.get(f"/repositories/{world.repo1}/snapshots")).status_code == 200
            assert (await bob.get(f"/snapshots/{world.keys[world.repo3][0]}")).status_code == 200
            denied = await bob.get(f"/snapshots/{world.keys[world.repo2][0]}")
            assert (denied.status_code, denied.json()) == (404, NOT_FOUND)

            # Carol's installation is disjoint from Alice's and Bob's.
            assert (await carol.get(f"/snapshots/{world.keys[world.repo2][0]}")).status_code == 200
            for key in world.keys[world.repo1] + world.keys[world.repo3]:
                response = await carol.get(f"/snapshots/{key}")
                assert (response.status_code, response.json()) == (404, NOT_FOUND)
            assert (await alice.get(f"/snapshots/{world.keys[world.repo2][0]}")).status_code == 404

            # Dave belongs to no installation at all.
            assert (await dave.get(f"/repositories/{world.repo1}/snapshots")).status_code == 404
            assert (await dave.get(f"/snapshots/{world.keys[world.repo1][0]}")).status_code == 404

            # An unreadable repository looks exactly like one that does not exist.
            unreadable = await carol.get(f"/repositories/{world.repo1}/snapshots")
            missing = await carol.get("/repositories/999999999999/snapshots")
            assert unreadable.status_code == missing.status_code == 404
            assert unreadable.json() == missing.json() == NOT_FOUND
            unknown_key = await carol.get("/snapshots/github:1:" + "a" * 40 + ":schema-1")
            assert unknown_key.json() == NOT_FOUND
        finally:
            for browser in (alice, bob, carol, dave):
                await browser.aclose()

    _run(scenario)


def test_revoked_grants_and_membership_changes_apply_on_the_next_request() -> None:
    async def scenario(world: World) -> None:
        alice = await world.browser_for("alice")
        bob = await world.browser_for("bob")
        try:
            key = world.keys[world.repo1][0]
            assert (await alice.get(f"/snapshots/{key}")).status_code == 200

            # Revoking one of Alice's two routes in leaves the other working.
            await world.revoke_grant(world.inst_a, world.repo1)
            assert (await alice.get(f"/snapshots/{key}")).status_code == 200
            assert (await alice.get(f"/repositories/{world.repo1}/snapshots")).status_code == 200

            # Revoking the second closes access, for Alice and for Bob.
            await world.revoke_grant(world.inst_b, world.repo1)
            for browser in (alice, bob):
                gone = await browser.get(f"/snapshots/{key}")
                assert (gone.status_code, gone.json()) == (404, NOT_FOUND)
                listing = await browser.get(f"/repositories/{world.repo1}/snapshots")
                assert (listing.status_code, listing.json()) == (404, NOT_FOUND)

            # Repository 3 is still granted to installation B. If GitHub stops listing B
            # for Bob, he loses access immediately with the same session cookie.
            repo3_key = world.keys[world.repo3][0]
            assert (await bob.get(f"/snapshots/{repo3_key}")).status_code == 200
            world.github.members[bob.token] = (world.people["bob"][0], "bob", [])
            assert (await bob.get(f"/snapshots/{repo3_key}")).status_code == 404
        finally:
            await alice.aclose()
            await bob.aclose()

    _run(scenario)


def test_request_supplied_identity_is_never_authorization_evidence() -> None:
    async def scenario(world: World) -> None:
        carol = await world.browser_for("carol")
        try:
            key = world.keys[world.repo1][0]
            alice_id = world.people["alice"][0]
            response = await carol.get(
                f"/snapshots/{key}",
                params={
                    "user_id": alice_id,
                    "installation_id": world.inst_a,
                    "principal_id": str(world.inst_a),
                },
                headers={
                    "x-github-user-id": str(alice_id),
                    "x-installation-id": str(world.inst_a),
                },
            )
            assert (response.status_code, response.json()) == (404, NOT_FOUND)
            listing = await carol.get(
                f"/repositories/{world.repo1}/snapshots",
                params={"installation_id": world.inst_a},
            )
            assert listing.status_code == 404
        finally:
            await carol.aclose()

    _run(scenario)


def test_missing_expired_revoked_and_mismatched_sessions_fail_closed() -> None:
    async def scenario(world: World) -> None:
        key = world.keys[world.repo1][0]
        url = f"/snapshots/{key}"

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="https://gitwise.test"
        ) as anonymous:
            assert (await anonymous.get(url)).status_code == 401
            anonymous.cookies.set(SESSION_COOKIE, "not-a-real-session")
            assert (await anonymous.get(url)).status_code == 401

        # Idle-expired session.
        expired = await world.browser_for("alice")
        async with world.sessions._engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE user_sessions SET idle_expires_at = now() - interval '1 second' "
                    "WHERE github_user_id = :u"
                ),
                {"u": world.people["alice"][0]},
            )
        assert (await expired.get(url)).status_code == 401
        await expired.aclose()

        # GitHub no longer accepts the stored token.
        revoked = await world.browser_for("bob")
        del world.github.members[revoked.token]
        assert (await revoked.get(url)).status_code == 401
        await revoked.aclose()

        # The token now belongs to a different user than the session: end every session.
        swapped = await world.browser_for("carol")
        other = await world.browser_for("carol")
        world.github.members[swapped.token] = (
            world.people["alice"][0],
            "alice",
            world.people["alice"][1],
        )
        assert (await swapped.get(url)).status_code == 401
        assert (await other.get(f"/snapshots/{world.keys[world.repo2][0]}")).status_code == 401
        await swapped.aclose()
        await other.aclose()

    _run(scenario)


def test_github_outage_and_missing_configuration_do_not_open_access() -> None:
    async def scenario(world: World) -> None:
        alice = await world.browser_for("alice")
        try:
            key = world.keys[world.repo1][0]
            world.github.down = True
            assert (await alice.get(f"/snapshots/{key}")).status_code == 503
            world.github.down = False
            assert (await alice.get(f"/snapshots/{key}")).status_code == 200

            main.app.state.genome_snapshot_store = None
            assert (await alice.get(f"/snapshots/{key}")).status_code == 503
            main.app.state.genome_snapshot_store = world.genome

            main.app.state.auth_components = None
            assert (await alice.get(f"/snapshots/{key}")).status_code == 503
        finally:
            await alice.aclose()

    _run(scenario)


def test_history_listing_orders_limits_and_validates_input() -> None:
    async def scenario(world: World) -> None:
        alice = await world.browser_for("alice")
        try:
            limited = await alice.get(
                f"/repositories/{world.repo1}/snapshots", params={"limit": 1}
            )
            assert limited.status_code == 200
            assert len(limited.json()["snapshots"]) == 1
            for bad in (0, 101, "x"):
                response = await alice.get(
                    f"/repositories/{world.repo1}/snapshots", params={"limit": bad}
                )
                assert response.status_code == 422
            assert (await alice.get("/repositories/0/snapshots")).status_code == 404
            assert (await alice.get("/repositories/-5/snapshots")).status_code == 404
            assert (await alice.get("/snapshots/" + "k" * 600)).status_code == 404
        finally:
            await alice.aclose()

    _run(scenario)
