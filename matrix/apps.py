"""外部应用接入（docs/open-api.md，不变量 30）。

企业里的业务系统接进来：查可用的 Agent 与模板、用它们组项目、把项目作为服务发布与调用。两种身份：
代表一个人（用户在“我的授权”里签令牌），或者应用自己的业务系统账号（管理员签令牌）。

规则（不变量 30）：
- 做的事等于令牌代表的主体本来就能做的，再交令牌的 scope 与项目范围（代表个人时：经这个应用建的，加上授权人交给它管的）；
- 令牌拿不到内容（对话、过程、私有目录、凭证），它自己调用服务的会话除外；
- 每次写操作都留下“经哪个应用”（档案日志 `via_app`、群聊事件后缀）；撤销 / 停用立即生效；
- 系统账号不能登录网页、不能新建 Agent、至少一名负责人；负责人在网页上对它主持的项目等同主持人；
- 开放给应用的个人 Agent 被拉进项目时，所有者成为那个项目的参与者（等价于受邀加入后带进自己的 Agent）。

这里是 Network 的一个混入（mixin），用 Network 的锁、存储与已有的操作；不另开一套权限。
"""

from __future__ import annotations

import contextlib
import hashlib
import secrets
import threading
import time
from typing import TYPE_CHECKING, Any, Iterator

from .models import APP_SCOPES, APP_TOKEN_PREFIX, AgentNode, AppGrant, ExternalApp, Project, User, new_id

if TYPE_CHECKING:  # pragma: no cover
    from .engine import Network

APPS_MAX = 200
GRANTS_PER_USER = 50
APP_CALLS_PER_MINUTE = 600


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class AppContext:
    """一次经应用令牌的调用：哪个应用、哪把令牌、代表谁（主体）。"""

    def __init__(self, app: ExternalApp, grant: AppGrant, user: User):
        self.app, self.grant, self.user = app, grant, user

    @property
    def principal(self) -> str:
        return self.user.id

    @property
    def system(self) -> bool:
        return self.user.is_system


class AppsMixin:
    """Network 的外部应用部分。"""

    _via_local = threading.local()

    # ================= 经应用留痕 =================
    @contextlib.contextmanager
    def via_app(self: "Network", app: ExternalApp) -> Iterator[None]:
        """这段代码里的写操作记为“经这个应用”：档案日志带 via_app，群聊事件后面写“（经「应用」）”。只对当前线程有效。"""
        prev = getattr(self._via_local, "app", None)
        self._via_local.app = app
        try:
            yield
        finally:
            self._via_local.app = prev

    def _via(self) -> ExternalApp | None:
        return getattr(self._via_local, "app", None)

    # ================= 身份：系统账号与负责人 =================
    def manages(self: "Network", actor: str | None, user_id: str | None) -> bool:
        """actor 是不是系统账号 user_id 的负责人。"""
        u = self.store.users.get(user_id or "")
        return bool(actor) and u is not None and u.is_system and actor in u.managers

    def _sync_managers(self: "Network", sysu: User, before: list[str]) -> None:
        """调用方持有锁。负责人变了：它主持的项目里，新负责人成为参与者，卸任的退出（他的 Agent 一并移出）。"""
        for g in self.store.projects.values():
            if g.owner_id != sysu.id:
                continue
            for uid in sysu.managers:
                if uid not in g.participants:
                    self._join_as_participant(g, uid, f"{self._email(uid)} 作为负责人加入了项目")
            for uid in before:
                if uid not in sysu.managers and uid in g.participants:
                    self._drop_participant(g, uid, f"{self._email(uid)} 不再是负责人，退出了项目")

    def _join_as_participant(self: "Network", g: Project, uid: str, text: str) -> None:
        """调用方持有锁。和接受邀请一样：成为参与者、群聊里记一条（助理不跟着进来，不变量 18）。"""
        if uid in g.people:
            return
        if not g.shared:
            self._start_sharing(g)
        g.invited = [x for x in g.invited if x != uid]
        g.participants.append(uid)
        self._people_event(g, text)

    # ================= 管理员：应用与系统账号 =================
    def _admin(self: "Network", actor: str | None) -> None:
        self._check(actor is None or self.is_admin(actor), "admin")

    def _clean_scopes(self, scopes: Any, limit: list[str] | None = None) -> list[str]:
        out = [s for s in dict.fromkeys(str(x).strip() for x in (scopes or [])) if s]
        bad = [s for s in out if s not in APP_SCOPES]
        if bad:
            from .engine import NetworkError
            raise NetworkError(f"没有这些授权范围：{'、'.join(bad)}")
        if limit is not None:
            over = [s for s in out if s not in limit]
            if over:
                from .engine import NetworkError
                raise NetworkError(f"这个应用不能申请：{'、'.join(over)}")
        return out

    def _user_ids_by_email(self: "Network", emails: Any) -> list[str]:
        from .engine import NetworkError
        out: list[str] = []
        for e in emails or []:
            e = str(e).strip().lower()
            if not e:
                continue
            u = next((x for x in self.store.users.values() if x.email == e), None)
            if u is None or u.is_system:
                raise NetworkError(f"没有用户 {e}")
            if u.id not in out:
                out.append(u.id)
        return out

    def app_view(self: "Network", app: ExternalApp, admin: bool = True) -> dict[str, Any]:
        """调用方持有锁。"""
        d = app.to_dict()
        sysu = self.store.users.get(app.system_user_id or "")
        if sysu is not None:
            d["system"] = {"id": sysu.id, "email": sysu.email, "managers": [self._email(u) for u in sysu.managers],
                           "token": next(({"id": g.id, "scopes": g.scopes, "created_at": g.created_at,
                                           "last_used_at": g.last_used_at, "calls": g.calls}
                                          for g in self.store.app_grants.values()
                                          if g.app_id == app.id and g.user_id == sysu.id and g.revoked_at is None), None),
                           "projects": [{"id": g.id, "name": g.name} for g in self.store.projects.values()
                                        if g.owner_id == sysu.id]}
        if admin:
            live = [g for g in self.store.app_grants.values() if g.app_id == app.id and g.revoked_at is None]
            d["grants"] = len(live)
            d["calls"] = sum(g.calls for g in self.store.app_grants.values() if g.app_id == app.id)
            d["last_used_at"] = max((g.last_used_at or 0 for g in live), default=0) or None
        return d

    def admin_apps(self: "Network", actor: str | None) -> list[dict[str, Any]]:
        self._admin(actor)
        with self._lock:
            return [self.app_view(a) for a in sorted(self.store.apps.values(), key=lambda a: a.created_at)]

    def create_app(self: "Network", actor: str | None, name: str, description: str = "", scopes: Any = None,
                   system: bool = False, managers: Any = None) -> ExternalApp:
        """管理员登记一个外部应用；system=True 时同时建它的业务系统账号（至少一名负责人）。"""
        from .engine import NetworkError
        self._admin(actor)
        name = " ".join(str(name or "").split())[:80]
        if not name:
            raise NetworkError("应用名不能为空")
        with self._lock:
            if len(self.store.apps) >= APPS_MAX:
                raise NetworkError(f"最多登记 {APPS_MAX} 个应用")
            if any(a.name == name for a in self.store.apps.values()):
                raise NetworkError(f"已经有一个叫 {name} 的应用")
            app = ExternalApp(id=new_id("app"), name=name, description=str(description or "").strip()[:1000],
                              scopes=self._clean_scopes(scopes or list(APP_SCOPES)), created_by=actor)
            if system:
                mids = self._user_ids_by_email(managers)
                if not mids:
                    raise NetworkError("业务系统账号至少要有一名负责人")
                u = User(id=new_id("user"), email=f"{app.id}@apps.matrix", password_hash="", salt="",
                         kind="system", managers=mids, app_id=app.id)
                self.store.users[u.id] = u
                app.system_user_id = u.id
            self.store.apps[app.id] = app
            self.store.save()
        return app

    def update_app(self: "Network", actor: str | None, app_id: str, **f: Any) -> ExternalApp:
        """改名、说明、授权范围上限、停用 / 启用、负责人。收窄范围时，已签的令牌跟着收窄。"""
        from .engine import NetworkError
        self._admin(actor)
        with self._lock:
            app = self.store.apps.get(app_id)
            self._check(app is not None, f"app {app_id}")
            if f.get("name") is not None:
                n = " ".join(str(f["name"]).split())[:80]
                if not n:
                    raise NetworkError("应用名不能为空")
                app.name = n
            if f.get("description") is not None:
                app.description = str(f["description"]).strip()[:1000]
            if f.get("scopes") is not None:
                app.scopes = self._clean_scopes(f["scopes"])
                for g in self.store.app_grants.values():
                    if g.app_id == app.id:
                        g.scopes = [s for s in g.scopes if s in app.scopes]
            if f.get("status") is not None:
                if f["status"] not in ("active", "disabled"):
                    raise NetworkError("status 只能是 active / disabled")
                app.status = f["status"]
            if f.get("managers") is not None:
                sysu = self.store.users.get(app.system_user_id or "")
                if sysu is None:
                    raise NetworkError("这个应用没有业务系统账号")
                mids = self._user_ids_by_email(f["managers"])
                if not mids:
                    raise NetworkError("业务系统账号至少要有一名负责人")
                before, sysu.managers = list(sysu.managers), mids
                self._sync_managers(sysu, before)
            self.store.save()
        return app

    def issue_system_token(self: "Network", actor: str | None, app_id: str, scopes: Any = None) -> tuple[AppGrant, str]:
        """管理员给应用的系统账号签令牌（只显示一次）；重签即作废旧的。"""
        from .engine import NetworkError
        self._admin(actor)
        with self._lock:
            app = self.store.apps.get(app_id)
            self._check(app is not None, f"app {app_id}")
            if app.system_user_id is None:
                raise NetworkError("这个应用没有业务系统账号")
            for g in self.store.app_grants.values():
                if g.app_id == app.id and g.user_id == app.system_user_id and g.revoked_at is None:
                    g.revoked_at = time.time()
            grant, token = self._new_grant(app, app.system_user_id, self._clean_scopes(scopes or app.scopes, app.scopes),
                                           [], actor)
            self.store.save()
        return grant, token

    def _new_grant(self: "Network", app: ExternalApp, user_id: str, scopes: list[str], projects: list[str],
                   by: str | None) -> tuple[AppGrant, str]:
        """调用方持有锁。"""
        token = APP_TOKEN_PREFIX + secrets.token_urlsafe(32)
        g = AppGrant(id=new_id("grant"), app_id=app.id, user_id=user_id, scopes=scopes, token_sha=_sha(token),
                     projects=projects, created_by=by)
        self.store.app_grants[g.id] = g
        return g, token

    # ================= 用户：我的授权、开放 Agent、负责的系统账号 =================
    def apps_for(self: "Network", actor: str | None) -> dict[str, Any]:
        """我能授权的应用、我签过的授权、我负责的系统账号。"""
        uid = self._need_user(actor)
        with self._lock:
            apps = [{"id": a.id, "name": a.name, "description": a.description,
                     "scopes": [{"key": s, "label": APP_SCOPES[s]} for s in a.scopes]}
                    for a in sorted(self.store.apps.values(), key=lambda a: a.created_at) if a.status == "active"]
            grants = [{**g.to_dict(), "app_name": self.store.apps[g.app_id].name if g.app_id in self.store.apps else "",
                       "project_names": [self.store.projects[p].name for p in g.projects if p in self.store.projects]}
                      for g in sorted(self.store.app_grants.values(), key=lambda g: -g.created_at)
                      if g.user_id == uid and g.revoked_at is None]
            managed = [self.app_view(a, admin=False) for a in self.store.apps.values()
                       if a.system_user_id and self.manages(uid, a.system_user_id)]
        return {"apps": apps, "grants": grants, "managed": managed, "scopes": dict(APP_SCOPES)}

    def grant_app(self: "Network", actor: str | None, app_id: str, scopes: Any, projects: Any = None) -> tuple[AppGrant, str]:
        """用户授权一个应用代表自己（令牌只显示一次）。projects：另外交给它管的、自己主持的已有项目。"""
        from .engine import NetworkError
        uid = self._need_user(actor)
        with self._lock:
            app = self.store.apps.get(app_id)
            self._check(app is not None and app.status == "active", f"app {app_id}")
            mine = [g for g in self.store.app_grants.values() if g.user_id == uid and g.revoked_at is None]
            if len(mine) >= GRANTS_PER_USER:
                raise NetworkError(f"最多 {GRANTS_PER_USER} 个授权")
            sc = self._clean_scopes(scopes, app.scopes)
            if not sc:
                raise NetworkError("至少选一个授权范围")
            pids = []
            for p in projects or []:
                g = self.hosted_project(str(p), uid)
                if g.owner_id != uid:
                    raise NetworkError(f"只能把你自己主持的项目交给应用：{g.name}")
                pids.append(g.id)
            grant, token = self._new_grant(app, uid, sc, list(dict.fromkeys(pids)), uid)
            self.store.save()
        return grant, token

    def revoke_grant(self: "Network", actor: str | None, grant_id: str) -> None:
        """撤销一把令牌：本人、管理员都能撤；立即作废。"""
        with self._lock:
            g = self.store.app_grants.get(grant_id)
            self._check(g is not None and g.revoked_at is None and (
                actor is None or g.user_id == actor or self.is_admin(actor)), f"grant {grant_id}")
            g.revoked_at = time.time()
            self.store.save()

    def set_agent_open(self: "Network", agent_id: str, app_ids: Any, actor: str | None = None) -> AgentNode:
        """所有者把自己的个人 Agent 开放给哪些应用（不变量 30）：之后这些应用查得到它、能把它拉进自己主持的项目。"""
        from .engine import NetworkError
        with self._lock:
            a = self.owned_agent(agent_id, actor)
            if actor is not None and a.owner_id != actor:
                raise NetworkError("只有所有者能把它开放给应用")
            ids = [x for x in dict.fromkeys(str(i) for i in (app_ids or [])) if x]
            missing = [x for x in ids if x not in self.store.apps]
            if missing:
                raise NetworkError(f"没有这些应用：{'、'.join(missing)}")
            a.open_to = ids
            self.store.save()
        self.bus.emit("agent.updated", agent=a.to_dict())
        return a

    def system_connections(self: "Network", actor: str | None, user_id: str) -> list[dict[str, Any]]:
        """负责人 / 管理员看系统账号的连接器（只有状态，不给凭证）。"""
        self._check_system_admin(actor, user_id)
        return self.my_connections(user_id)

    def authorize_system_connector(self: "Network", actor: str | None, user_id: str, connector_id: str,
                                   values: dict[str, Any]) -> dict[str, Any]:
        """负责人代系统账号授权一个目录连接器（管理员也能配）：凭证照旧只进保管库，归系统账号。"""
        self._check_system_admin(actor, user_id)
        return self.authorize_connector(connector_id, values, user_id)

    def _check_system_admin(self: "Network", actor: str | None, user_id: str) -> None:
        u = self.store.users.get(user_id)
        self._check(u is not None and u.is_system and (actor is None or self.manages(actor, u.id) or self.is_admin(actor)),
                    f"user {user_id}")

    # ================= 令牌 =================
    def app_context(self: "Network", token: str | None, scope: str | tuple[str, ...]) -> AppContext:
        """凭令牌认出应用与主体，并检查 scope（给几个时有一个就行）。认不出 / 撤销 / 停用一律当不存在。"""
        from .engine import NetworkError
        if not token or not token.startswith(APP_TOKEN_PREFIX):
            raise NetworkError("unknown token")
        sha = _sha(token)
        with self._lock:
            g = next((x for x in self.store.app_grants.values() if x.token_sha == sha and x.revoked_at is None), None)
            app = self.store.apps.get(g.app_id) if g else None
            user = self.store.users.get(g.user_id) if g else None
            if g is None or app is None or user is None or app.status != "active":
                raise NetworkError("unknown token")
            now = time.time()
            hits = [t for t in self._app_hits.get(g.id, []) if now - t < 60]
            if len(hits) >= APP_CALLS_PER_MINUTE:
                raise NetworkError("调用太频繁，稍后再试")
            self._app_hits[g.id] = hits + [now]
            wanted = (scope,) if isinstance(scope, str) else scope
            if not any(x in g.scopes for x in wanted):
                raise NetworkError(f"这把令牌没有授权 {' / '.join(wanted)}")
            g.calls += 1
            g.last_used_at = now
        return AppContext(app, g, user)

    # ================= 查 =================
    def open_agents(self: "Network", c: AppContext) -> list[dict[str, Any]]:
        """能用的 Agent：主体本来就能拉进项目的（自己的、公共的），加上所有者开放给这个应用的。只给名片。"""
        with self._lock:
            out = []
            for a in sorted(self.store.agents.values(), key=lambda a: a.created_at):
                usable = self.can_call_agent(a, c.principal)
                opened = c.app.id in a.open_to and not a.is_public
                if not (usable or opened):
                    continue
                spec = self.platforms.get(a.remote.get("platform")) if a.remote else None
                out.append({"id": a.id, "name": a.name, "description": a.description,
                            "capabilities": list(a.capabilities),
                            "authority": list(a.public_authority if a.is_public else a.authority),
                            "kind": "public" if a.is_public else "mine" if a.owner_id == c.principal else "opened",
                            "owner": self._email(a.owner_id) if a.owner_id else None,
                            "connectors": list(a.public_connectors) if a.is_public else [],
                            "where": "remote" if a.remote else "device" if a.device_runtime else "cloud",
                            "platform": spec.name if spec else None,
                            "online": self.device_online(a) if a.device_runtime else True})
            return out

    def open_blueprints(self: "Network", c: AppContext) -> list[dict[str, Any]]:
        return self.blueprint_list(c.principal)

    # ================= 项目范围 =================
    def _app_project(self: "Network", c: AppContext, project_id: str) -> Project:
        """调用方持有锁。令牌能管的项目：主体主持的，而且（系统账号）是它自己的、（代表个人）经这个应用建的或交给它管的。"""
        g = self.store.projects.get(project_id)
        ok = g is not None and g.owner_id == c.principal and (
            c.system or g.via_app == c.app.id or g.id in c.grant.projects)
        self._check(ok, f"project {project_id}")
        return g

    def open_projects(self: "Network", c: AppContext) -> list[dict[str, Any]]:
        with self._lock:
            return [self._open_project_view(g) for g in sorted(self.store.projects.values(), key=lambda g: g.created_at)
                    if g.owner_id == c.principal and (c.system or g.via_app == c.app.id or g.id in c.grant.projects)]

    def _open_project_view(self: "Network", g: Project) -> dict[str, Any]:
        """调用方持有锁。令牌看到的项目：配置，不给内容（群聊、会话、过程）。"""
        return {"id": g.id, "name": g.name, "description": g.description, "brief": g.brief,
                "members": [{"id": m, "name": self._name(m)} for m in g.member_ids],
                "participants": [self._email(u) for u in g.participants],
                "roles": {k: {"name": r.get("name"), "members": list(r.get("members") or [])} for k, r in g.roles.items()},
                "files": [f["name"] for f in self.project_files(g)],
                "skills": sorted(g.skills), "plugins": sorted(g.plugins),
                "services": [self.service_view(x, None) for x in self.store.project_services.values() if x.project_id == g.id],
                "via_app": g.via_app, "created_at": g.created_at}

    def open_project(self: "Network", c: AppContext, project_id: str) -> dict[str, Any]:
        with self._lock:
            return self._open_project_view(self._app_project(c, project_id))

    # ================= 组 =================
    def open_create_project(self: "Network", c: AppContext, name: str, description: str = "", brief: str = "",
                            members: Any = None, blueprint: str | None = None, params: dict[str, Any] | None = None,
                            roles: dict[str, Any] | None = None, digest: str | None = None) -> dict[str, Any]:
        """建项目：主体当主持人，经这个应用。成员按 id；开放给应用的个人 Agent 拉进来时所有者成为参与者。
        blueprint：再用模板装进去，模板里的每个角色都要指给项目里的成员（应用不新建 Agent，决策 5）；
        第一次不带 digest 时返回要确认的事（NeedsConfirm），带上 digest 再来一次才装。"""
        from .engine import NetworkError
        name = " ".join(str(name or "").split())[:80]
        if not name:
            raise NetworkError("项目名不能为空")
        ids = list(dict.fromkeys(str(m) for m in (members or [])))
        with self._lock:  # 先查成员：拉不进来的不建项目
            for m in ids:
                a = self.store.agents.get(m)
                self._check(a is not None and (self.can_call_agent(a, c.principal) or c.app.id in a.open_to), f"agent {m}")
        if blueprint:
            self._check_blueprint_roles(c, blueprint, roles, ids)
        with self.via_app(c.app):
            with self._lock:
                direct = [m for m in ids if (a := self.store.agents.get(m)) is not None and self.can_call_agent(a, c.principal)]
            g = self.create_project(name, description, direct, actor=c.principal)
            with self._lock:
                g.via_app = c.app.id
                sysu = c.user
                if sysu.is_system:  # 负责人成为参与者：在网页上对这个项目等同主持人
                    for uid in sysu.managers:
                        self._join_as_participant(g, uid, f"{self._email(uid)} 作为负责人加入了项目")
                self.store.save()
            try:
                for m in ids:
                    if m not in direct:
                        self.open_add_member(c, g.id, m)
                if brief:
                    self.set_brief(g.id, brief, actor=c.principal)
                if blueprint:
                    self.create_project_from_blueprint(
                        blueprint, name, params, {k: {"agent": v} for k, v in (roles or {}).items()},
                        confirm=bool(digest), digest=digest, actor=c.principal, project_id=g.id)
            except Exception:
                if blueprint and not digest:  # 第一次只是看要确认什么：项目不留
                    self._drop_open_project(g)
                raise
        return self.open_project(c, g.id)

    def _drop_open_project(self: "Network", g: Project) -> None:
        with self._lock:
            self.store.projects.pop(g.id, None)
            gone = [cid for cid, conv in self.store.conversations.items() if conv.project_id == g.id]
            for cid in gone:
                del self.store.conversations[cid]
            self.store.save()

    def _check_blueprint_roles(self: "Network", c: AppContext, bid: str, roles: Any, members: list[str]) -> None:
        from . import blueprints as bps
        from .engine import NetworkError
        meta = self._bp_meta(bid, c.principal)
        bp = bps.check_package(self.blueprints.files(meta["id"]))
        roles = roles or {}
        missing = [r.name for r in bp.roles if r.key not in roles]
        if missing:
            raise NetworkError(f"应用不能新建 Agent：模板里的这些角色要指给项目成员：{'、'.join(missing)}")
        bad = [k for k, v in roles.items() if str(v) not in members]
        if bad:
            raise NetworkError(f"角色要指给这个项目的成员（members 里的）：{'、'.join(bad)}")

    def open_add_member(self: "Network", c: AppContext, project_id: str, agent_id: str) -> dict[str, Any]:
        """拉一个成员进来：主体本来就能拉的照常；开放给这个应用的个人 Agent，所有者先成为参与者，再由他带进来。"""
        with self.via_app(c.app):
            with self._lock:
                g = self._app_project(c, project_id)
                a = self.store.agents.get(agent_id)
                self._check(a is not None and (self.can_call_agent(a, c.principal) or c.app.id in a.open_to),
                            f"agent {agent_id}")
                opened = not self.can_call_agent(a, c.principal)
                if opened:
                    self._join_as_participant(g, a.owner_id, f"{self._email(a.owner_id)} 加入了项目"
                                              f"（他的「{a.name}」开放给了「{c.app.name}」）")
                    self.store.save()
            self.add_member(g.id, a.id, actor=a.owner_id if opened else c.principal)
        return self.open_project(c, project_id)

    def open_delete_project(self: "Network", c: AppContext, project_id: str) -> None:
        """删项目：只能删经这个应用建的（授权人另外交给它管的已有项目，只能管、不能删）。规则同主持人删除：
        有进行中的对话时不能删；发布物与服务一并撤回，服务会话一起删，群聊归档为只读。档案日志里留一条“经哪个应用删的”。"""
        from .engine import NetworkError
        with self.via_app(c.app):
            with self._lock:
                g = self._app_project(c, project_id)
                if g.via_app != c.app.id:
                    raise NetworkError("只能删经这个应用建的项目：这个项目是授权人交给它管的，删除请在 Matrix 里由主持人操作")
                self._archive_changed(g, "project.delete", by_user=c.principal)
            self.delete_project(g.id, actor=c.principal)

    def open_remove_member(self: "Network", c: AppContext, project_id: str, agent_id: str) -> dict[str, Any]:
        with self.via_app(c.app):
            with self._lock:
                g = self._app_project(c, project_id)
            self.remove_member(g.id, agent_id, actor=c.principal)
        return self.open_project(c, project_id)

    def open_update_project(self: "Network", c: AppContext, project_id: str, description: str | None = None,
                            brief: str | None = None, roles: dict[str, Any] | None = None) -> dict[str, Any]:
        with self.via_app(c.app):
            with self._lock:
                g = self._app_project(c, project_id)
            if description is not None:
                self.update_project(g.id, description=description, actor=c.principal)
            if brief is not None:
                self.set_brief(g.id, brief, actor=c.principal)
            if roles is not None:
                self.set_project_roles(g.id, roles, actor=c.principal)
        return self.open_project(c, project_id)

    def open_put_file(self: "Network", c: AppContext, project_id: str, path: str, data: bytes) -> dict[str, Any]:
        with self.via_app(c.app):
            with self._lock:
                g = self._app_project(c, project_id)
            name = self.publish_file(g.id, path, data, actor=c.principal)
        return {"name": name}

    def open_file_access(self: "Network", c: AppContext, project_id: str, path: str, rule: Any) -> dict[str, Any]:
        with self.via_app(c.app):
            with self._lock:
                g = self._app_project(c, project_id)
            self.set_file_access(g.id, path, rule, actor=c.principal)
        return self.open_project(c, project_id)

    def open_save_skill(self: "Network", c: AppContext, project_id: str, name: str, description: str,
                        body: str) -> dict[str, Any]:
        with self.via_app(c.app):
            with self._lock:
                g = self._app_project(c, project_id)
            return self.save_project_skill(g.id, name, description, body, actor=c.principal)

    def open_install_plugin(self: "Network", c: AppContext, project_id: str, name: str,
                            digest: str | None = None) -> dict[str, Any]:
        """只从部署方目录装（不变量 20、24 的现有规则照旧）；第一次不带 digest 时返回要确认的事。"""
        with self.via_app(c.app):
            with self._lock:
                g = self._app_project(c, project_id)
            return self.install_plugin(g.id, name, confirm=bool(digest), actor=c.principal, digest=digest)

    # ================= 发 =================
    def open_create_service(self: "Network", c: AppContext, project_id: str, **f: Any) -> dict[str, Any]:
        with self.via_app(c.app):
            with self._lock:
                g = self._app_project(c, project_id)
            x = self.create_service(g.id, f.pop("name", ""), f.pop("title", ""), f.pop("description", ""),
                                    actor=c.principal, **f)
        with self._lock:
            return self.service_view(x, None)

    def open_update_service(self: "Network", c: AppContext, project_id: str, service_id: str, **f: Any) -> dict[str, Any]:
        with self.via_app(c.app):
            with self._lock:
                g = self._app_project(c, project_id)
            x = self.update_service(g.id, service_id, actor=c.principal, **f)
        with self._lock:
            return self.service_view(x, None)

    def open_withdraw_service(self: "Network", c: AppContext, project_id: str, service_id: str) -> dict[str, Any]:
        with self.via_app(c.app):
            with self._lock:
                g = self._app_project(c, project_id)
            x = self.withdraw_service(g.id, service_id, actor=c.principal)
        with self._lock:
            return self.service_view(x, None)

    def open_service_usage(self: "Network", c: AppContext, project_id: str, service_id: str, days: int = 30) -> dict[str, Any]:
        with self._lock:
            g = self._app_project(c, project_id)
        return self.service_usage(g.id, service_id, days, actor=c.principal)

    # ================= 调：项目服务的标准 HTTP 形状（第 6 节，项目服务第二期） =================
    def svc_card(self: "Network", c: AppContext, service_id: str) -> dict[str, str]:
        """GET /svc/agents/{service}：名片。只认调用方看得到的、在发布中的服务。"""
        from .engine import NetworkError
        with self._lock:
            x = self.visible_service(service_id, c.principal)
            if x.status != "active":
                raise NetworkError(f"unknown service {service_id}")
            return {"name": x.title, "description": x.description}

    def svc_session(self: "Network", c: AppContext, service_id: str, session_id: str | None):
        """POST /svc/agents/{service}/chat：续接调用方自己的服务会话，或新开一个。会话 id 就是 Matrix 的对话 id。"""
        if session_id:
            with self._lock:
                conv = self.store.conversations.get(session_id)
                self._check(conv is not None and conv.kind == "service" and conv.owner_id == c.principal
                            and conv.service_id == service_id, f"session {session_id}")
            return conv
        return self.open_service_session(service_id, actor=c.principal)

    def svc_interrupt(self: "Network", c: AppContext, session_id: str) -> None:
        from .engine import NetworkError
        with self._lock:
            conv = self.store.conversations.get(session_id)
            self._check(conv is not None and conv.kind == "service" and conv.owner_id == c.principal,
                        f"session {session_id}")
            agent = conv.agent_id
        try:
            self.stop_run(f"{session_id}/{agent}", actor=c.principal)
        except NetworkError as e:
            if not str(e).startswith("这一轮已经结束"):
                raise
