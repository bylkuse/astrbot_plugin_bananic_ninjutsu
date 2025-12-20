import time
from typing import List, Optional, Dict, Any

from ..domain import PromptResolver, ApiRequest, GenResult, GenerationConfig, PluginError, APIErrorType, QuotaTransaction
from ..providers import ProviderManager
from ..utils import Result, Ok, Err
from . import ConfigService, StatsService

class GenerationService:
    def __init__(
        self, 
        provider_manager: ProviderManager,
        config_service: "ConfigService",
        stats_service: "StatsService",
        prompt_resolver: PromptResolver
    ):
        self.provider_mgr = provider_manager
        self.config_service = config_service
        self.stats_service = stats_service
        self.prompt_resolver = prompt_resolver

    async def generate_image(
        self,
        ctx_map: Dict[str, Any],
        gen_config: GenerationConfig,
        image_bytes: List[bytes],
        preset_name: Optional[str] = None,
        proxy_url: Optional[str] = None
    ) -> Result[GenResult, PluginError]:

        # 1. 连接预设
        conn_preset = self.config_service.get_active_preset()
        if not conn_preset:
            return Err(PluginError(APIErrorType.INVALID_ARGUMENT, "未配置任何有效的连接预设，请联系管理员配置。"))

        # 2. 提示词预处理
        try:
            final_prompt = self.prompt_resolver.resolve(
                gen_config.prompt, 
                gen_config.to_dict(),
                ctx_map
            )

            gen_config.prompt = final_prompt
        except Exception as e:
            return Err(PluginError(APIErrorType.INVALID_ARGUMENT, f"提示词解析失败: {e}"))

        # 3. 配额检查
        user_id = ctx_map.get("user_id", "0")
        group_id = ctx_map.get("group_id")
        is_admin = ctx_map.get("is_admin", False)

        # 计算消耗
        cost = 1
        if "4K" in gen_config.image_size.upper(): cost = 4
        elif "2K" in gen_config.image_size.upper(): cost = 2

        quota_ctx = await self.stats_service.get_quota_context(user_id, group_id, is_admin)
        transaction = QuotaTransaction()

        if not transaction.check_permission(quota_ctx, cost):
            return Err(PluginError(APIErrorType.QUOTA_EXHAUSTED, transaction.reject_reason))

        # 4. 构建请求
        api_request = ApiRequest(
            api_key="",
            preset=conn_preset,
            gen_config=gen_config,
            image_bytes_list=image_bytes,
            proxy_url=proxy_url,
            debug_mode=self.config_service.is_debug_mode()
        )

        # 5. 调用 API
        start_time = time.time()
        result = await self.provider_mgr.generate(api_request)
        elapsed = time.time() - start_time

        # 6. 后处理
        if result.is_ok():
            gen_res = result.unwrap()
            gen_res.cost_time = elapsed
            new_u_bal, new_g_bal = transaction.commit(quota_ctx)
            gen_res.actual_cost = transaction.real_cost
            await self.stats_service.update_balance(user_id, group_id, new_u_bal, new_g_bal)
            await self.stats_service.record_usage(user_id, group_id, success=True)

            return Ok(gen_res)
        else:
            error = result.error
            transaction.rollback()

            await self.stats_service.record_usage(user_id, group_id, success=False)

            return Err(error)