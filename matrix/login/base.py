"""登录驱动接口：一个驱动只管和某个身份系统（公司的统一登录）对话，回答“回调带回来的这个人是谁”。

规则都在 service.py 与 auth.sso_login：一次登录只认发起它的那个浏览器、身份怎么对到 Matrix 用户、谁能进来、
登录状态由 Matrix 自己发。驱动不用、也不该自己做这些判断。

内置驱动：通用 OAuth2 授权码（oauth2.py）。公司的登录不是它能配出来的（自研的票据、CAS……）时写成独立的 Python 包，
用 entry point「matrix.login_drivers」注册一个 Driver 子类（见 docs/login-driver-authoring.md 与 registry.py）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class LoginError(Exception):
    """这次登录不成（身份系统拒绝、凭据换不到身份、配置不对……）。消息会显示在登录页上：写给人看，不要带密钥与令牌。"""


@dataclass(frozen=True)
class Identity:
    """身份系统说的“这是谁”。"""

    subject: str                         # 这个人在身份系统里不会变的标识（工号、用户 id）：绑定就绑它，不要用会改的名字
    email: str                           # 邮箱：Matrix 里邀请、认管理员都按它；第一次登录时用它建账号
    name: str = ""                       # 显示名（目前只用于提示）


@dataclass
class Flow:
    """一次进行中的登录。只在服务器内存里，10 分钟内有效，用过即作废。"""

    state: str                           # 随机串：必须跟着回调原样回来（见 Driver.state_param），服务据此确认回调属于这次登录
    redirect_uri: str                    # 回调地址（要在身份系统那边登记的就是它）
    data: dict[str, str] = field(default_factory=dict)   # 驱动这次登录要记的东西（如 PKCE 的 verifier），不出服务器

    @property
    def return_url(self) -> str:
        """带着 state 的回调地址。身份系统不会原样带回 state 参数、只会跳回你给的地址时（票据式的登录），把它当回跳地址。"""
        return f"{self.redirect_uri}?state={self.state}"


class Driver:
    """登录驱动的基类。子类声明 kind / title，实现 authorize_url / identify。

    构造时拿到部署方在 login.json 里给这个入口写的 `config`（`${环境变量}` 已经换成值）。构造函数里只校验配置、
    不要连网；配置不对抛 LoginError，这个入口就不出现在登录页上，原因打在服务日志里。
    两个方法都可能被多个请求同时调用：这次登录的状态放进 flow.data，不要放在 self 上。
    """

    kind: str = ""
    title: str = ""                      # 驱动的名字（写给部署方看；登录页上的按钮用 login.json 里的 name）
    state_param: str = "state"           # 回调的查询参数里，哪一个带着 flow.state

    def __init__(self, config: dict[str, Any]):
        self.config = config

    def authorize_url(self, flow: Flow) -> str:
        """浏览器要跳去的登录地址。必须让 flow.state 跟着回调回来。"""
        raise NotImplementedError

    def identify(self, flow: Flow, params: dict[str, str]) -> Identity:
        """回调的查询参数 → 是谁。在这里向身份系统核实（换令牌、验票据），核实不了抛 LoginError。
        只信从身份系统直接拿到的结果，不信浏览器带来的用户信息。"""
        raise NotImplementedError
