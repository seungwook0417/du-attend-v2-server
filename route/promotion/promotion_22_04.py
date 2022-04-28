import copy
import json
import os
import re
import secrets
import shutil

import bcrypt
import diskcache
from fastapi import APIRouter
from sentry_sdk import start_transaction, start_span
from sqlitedict import SqliteDict
from starlette.requests import Request
from starlette.templating import Jinja2Templates

from VO.account_vo import ChangePasswordAction, AccountAction
from route.user.account import validate
from service.login import login
from service.register import register
import logging

router = APIRouter()
templates = Jinja2Templates(directory="static")

os.makedirs("./promotion_db/", exist_ok=True)
PROMO_DB = SqliteDict('./promotion_db/promo_2204.sqlite', autocommit=False)
PROMO_LINK_DB = diskcache.Cache("./promotion_db/promo_2204_link/")

# 사용자 로그인에 필요한 데이터
USER_DB = SqliteDict('./database.sqlite', autocommit=False)
TOKEN_CACHE = diskcache.FanoutCache("./token_cache")

# 로깅 모듈
logFormatter = logging.Formatter(
    "%(asctime)s [%(levelname)-7.7s] [%(threadName)-12.12s] [%(funcName)-20.20s] %(message)s")
promo_logger = logging.getLogger(name="promotion")
promo_logger.setLevel(logging.INFO)

fileHandler = logging.FileHandler("./promotion_db/promotion.log", encoding="utf-8")
fileHandler.setFormatter(logFormatter)
promo_logger.addHandler(fileHandler)

consoleHandler = logging.StreamHandler()
consoleHandler.setFormatter(logFormatter)
promo_logger.addHandler(consoleHandler)


@router.post("/check/")
async def promotion_check(request: Request, action: AccountAction):
    with start_transaction(op="promotion_2204_check", name=f"promotion_2204_check") as transaction:
        login_result = login(action)
        if not login_result.success:
            promo_logger.warning(f"{action.std_id} {login_result.code} 로그인에 실패했습니다.")
            return login_result

        promotion_dict = PROMO_DB.get(action.std_id, None)
        promo_logger.info(f"{action.std_id} 프로모션 동의 상태: {promotion_dict is not None}")
        return {
            "success": True,
            "accept": promotion_dict is not None,
            "message": promotion_dict["link"] if promotion_dict is not None else ""
        }


@router.post("/get_code/")
async def promotion_get_url(request: Request, action: AccountAction):
    with start_transaction(op="promotion_2204_make_code", name=f"promotion_2204_make_code") as transaction:
        login_result = login(action)
        if not login_result.success:
            promo_logger.warning(f"{action.std_id} {login_result.code} 로그인에 실패했습니다.")
            return login_result

        promotion_code = secrets.token_hex(4)
        promotion_dict = {
            "link": promotion_code,
            "register_ids": set(),
            "viewer_ips": set(),
        }

        promotion_dict = PROMO_DB.get(action.std_id, None)

        if promotion_dict is None:
            promotion_dict = {
                "link": promotion_code,
                "register_ids": set(),
                "viewer_ips": set(),
            }
            PROMO_DB[action.std_id] = promotion_dict
            PROMO_LINK_DB.set(key=promotion_dict["link"], value=action.std_id)
            PROMO_DB.commit()
            promo_logger.info(f"{action.std_id} {promotion_dict['link']} 프로모션 링크를 새로 만들었습니다.")

        login_result.message = promotion_dict["link"]
        promo_logger.info(f"{action.std_id} {promotion_dict['link']} 프로모션 링크를 받았습니다.")
        return login_result


@router.get("/{code}")
async def promotion_page(request: Request, code: str):
    with start_transaction(op="promotion_2204_page", name=f"promotion_2204_page") as transaction:
        client_ip = request.client.host
        add_action_promotion(promo_id=code, mode=2, peer=client_ip)

        return templates.TemplateResponse("promotion_202204.html", {
            "trace": transaction.to_traceparent(),
            "request": request,
            "promo_code": code
        })


@router.post("/register")
async def promotion_register(request: Request, action: AccountAction, promo_id: str):
    with start_transaction(op="promotion_2204_register", name=f"promotion_2204_register") as transaction:
        register_result = register(action=action)
        if not register_result.success:
            return register_result

        add_action_promotion(promo_id=promo_id, mode=1, peer=action.std_id)

        return register_result


def add_action_promotion(promo_id, mode: int, peer: str):
    # mode 1: register_ids, mode 2: viewer_ips
    original_std_id = PROMO_LINK_DB.get(promo_id)

    if original_std_id:
        promo_logger.info(f"{peer} 가 방문했습니다. {promo_id} 의 주인은 {original_std_id} 입니다. ")
        promotion_dict = PROMO_DB[original_std_id]
        if mode == 1:
            register_ids: set = promotion_dict["register_ids"]
            register_ids.add(peer)

        if mode == 2:
            viewer_ips: set = promotion_dict["viewer_ips"]
            viewer_ips.add(peer)

        PROMO_DB[original_std_id] = promotion_dict
        PROMO_DB.commit()
        promo_logger.info(f"{mode} 모드로 저장했습니다. {original_std_id} 의 상태는 {promotion_dict} 입니다.")
    else:
        promo_logger.info(f"{peer} 가 방문했지만. {promo_id} 의 주인은 {original_std_id} 입니다. ")
