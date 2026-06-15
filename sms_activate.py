"""
Cliente para a API do sms-activate.org — serviço WhatsApp.

Documentação: https://sms-activate.org/en/api2

Uso:
    client = SmsActivate(api_key="SUA_CHAVE")
    act = await client.get_number(country=73)   # 73 = Brasil
    # ... Instagram envia código para act.phone ...
    code = await client.wait_code(act.id)
    await client.set_done(act.id)
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import urllib.request
import urllib.parse
import json

logger = logging.getLogger(__name__)

BASE = "https://api.sms-activate.org/stubs/handler_api.php"

# Países recomendados para Instagram/WhatsApp (menos saturados)
COUNTRIES = {
    "brasil":     73,
    "indonesia":  12,
    "mexico":     44,
    "india":       22,
    "colombia":   57,
    "any":         0,
}


@dataclass
class Activation:
    id: str
    phone: str      # número sem + (ex: 5511999991234)
    country: int


class SmsActivateError(Exception):
    pass


class SmsActivate:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def _get(self, **params) -> str:
        params["api_key"] = self.api_key
        url = BASE + "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=15) as r:
            return r.read().decode().strip()

    async def _aget(self, **params) -> str:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: self._get(**params))

    async def balance(self) -> float:
        r = await self._aget(action="getBalance")
        if r.startswith("ACCESS_BALANCE:"):
            return float(r.split(":")[1])
        raise SmsActivateError(f"balance error: {r}")

    async def get_number(
        self,
        country: int = 73,
        service: str = "wa",      # "wa" = WhatsApp
        max_price: Optional[float] = None,
    ) -> Activation:
        """
        Compra um número para ativação WhatsApp.
        country: use COUNTRIES dict acima.
        """
        params = dict(action="getNumber", service=service, country=country)
        if max_price:
            params["maxPrice"] = max_price
            params["action"] = "getNumberV2"

        r = await self._aget(**params)

        if r == "NO_NUMBERS":
            raise SmsActivateError("Sem números disponíveis para este país/serviço")
        if r == "NO_BALANCE":
            raise SmsActivateError("Saldo insuficiente no sms-activate")
        if r.startswith("ACCESS_NUMBER:"):
            _, act_id, phone = r.split(":")
            return Activation(id=act_id, phone=phone, country=country)
        # getNumberV2 retorna JSON
        try:
            data = json.loads(r)
            return Activation(
                id=str(data["activationId"]),
                phone=str(data["phoneNumber"]),
                country=country,
            )
        except Exception:
            raise SmsActivateError(f"get_number inesperado: {r}")

    async def get_status(self, activation_id: str) -> tuple[str, Optional[str]]:
        """
        Retorna (status, code_or_None).
        Possíveis status: WAIT_CODE, STATUS_OK, STATUS_CANCEL, STATUS_WAIT_RETRY
        """
        r = await self._aget(action="getStatus", id=activation_id)
        if ":" in r:
            status, code = r.split(":", 1)
            return status, code
        return r, None

    async def wait_code(
        self,
        activation_id: str,
        timeout: int = 120,
        poll_interval: int = 4,
    ) -> str:
        """
        Aguarda o código chegar (polling). Lança SmsActivateError se timeout.
        """
        elapsed = 0
        while elapsed < timeout:
            status, code = await self.get_status(activation_id)
            if status == "STATUS_OK" and code:
                logger.info(f"Código recebido para ativação {activation_id}: {code}")
                return code
            if status == "STATUS_CANCEL":
                raise SmsActivateError("Ativação cancelada pelo serviço")
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
        raise SmsActivateError(f"Timeout aguardando código (>{timeout}s)")

    async def set_done(self, activation_id: str):
        """Marca ativação como concluída (libera o número)."""
        await self._aget(action="setStatus", status="6", id=activation_id)

    async def set_cancel(self, activation_id: str):
        """Cancela a ativação (estorna o crédito se código não chegou)."""
        await self._aget(action="setStatus", status="8", id=activation_id)
