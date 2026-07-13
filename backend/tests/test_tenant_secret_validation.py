import pytest
from fastapi import HTTPException

from app.api.tenants import _validated_tenant_name
from app.core.secret_detection import looks_like_secret


@pytest.mark.parametrize(
    "value",
    [
        "sk-example_ABCDEFGHIJKLMNOP123456",
        "Bearer example_ABCDEFGHIJKLMNOP123456",
        "api_key=example_ABCDEFGHIJKLMNOP123456",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.signature123456789",
        "aB9dE2fG7hJ4kL8mN3pQ6rS1tV5xY0zC",
    ],
)
def test_secret_like_company_names_are_rejected_without_echoing_value(value: str):
    assert looks_like_secret(value)

    with pytest.raises(HTTPException) as exc:
        _validated_tenant_name(value)

    assert exc.value.status_code == 400
    assert value not in str(exc.value.detail)


@pytest.mark.parametrize(
    "value",
    [
        "深圳前海瑞孚图腾科技有限公司",
        "Reef Totem Technology Co., Ltd.",
        "OpenAI Research 2026",
    ],
)
def test_normal_company_names_remain_valid(value: str):
    assert not looks_like_secret(value)
    assert _validated_tenant_name(f"  {value}  ") == value
