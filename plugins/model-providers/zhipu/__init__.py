"""Zhipu (GLM) provider profile."""

from providers import register_provider
from providers.base import ProviderProfile

zhipu = ProviderProfile(
    name="zhipu",
    aliases=("glm", "chatglm"),
    env_vars=("ZHIPU_API_KEY",),
    display_name="Zhipu AI",
    description="Zhipu AI — GLM series models",
    signup_url="https://open.bigmodel.cn/",
    fallback_models=(
        "glm-4.5",
        "glm-4-flash",
    ),
    base_url="https://open.bigmodel.cn/api/paas/v4",
)

register_provider(zhipu)
