from __future__ import annotations

import asyncio
import os
import typing as t
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
import prometheus_client
import pydantic
from fastapi import FastAPI
from PIL.Image import Image as PILImage
from PIL.Image import fromarray
from starlette.requests import Request

import bentoml
from bentoml.io import JSON
from bentoml.io import File
from bentoml.io import Image
from bentoml.io import Multipart
from bentoml.io import NumpyNdarray
from bentoml.io import PandasDataFrame
from bentoml.io import Text

if TYPE_CHECKING:
    from numpy.typing import NDArray
    from starlette.types import ASGIApp
    from starlette.types import Receive
    from starlette.types import Scope
    from starlette.types import Send

    from bentoml._internal.types import FileLike
    from bentoml._internal.types import JSONSerializable


py_model = (
    bentoml.picklable_model.get("py_model.case-1.http.e2e")
    .with_options(partial_kwargs={"predict_ndarray": dict(coefficient=2)})
    .to_runner()
)


class StreamRunnable(bentoml.legacy.Runnable):
    SUPPORTED_RESOURCES = ("cpu",)
    SUPPORTS_CPU_MULTI_THREADING = True

    @bentoml.legacy.Runnable.method()
    async def count_text_stream(self, input_text: str) -> t.AsyncGenerator[str, None]:
        for i in range(10):
            await asyncio.sleep(0.1)
            yield f"{input_text} {i}"


stream_runner = bentoml.legacy.Runner(StreamRunnable)

svc = bentoml.legacy.Service(
    name="general_http_service.case-1.e2e", runners=[py_model, stream_runner]
)


metric_test = prometheus_client.Counter(
    name="test_metrics", documentation="Counter test metric"
)


@svc.api(input=bentoml.io.Text(), output=bentoml.io.Text())
def echo_data_metric(data: str) -> str:
    metric_test.inc()
    return data


@svc.api(input=JSON(), output=JSON())
async def echo_delay(data: dict[str, t.Any]) -> JSONSerializable:
    ret = await py_model.echo_delay.async_run(data)
    return ret


@svc.api(input=bentoml.io.Text(), output=bentoml.io.Text())
def ensure_metrics_are_registered(data: str) -> str:  # pylint: disable=unused-argument
    from prometheus_client import generate_latest
    from prometheus_client.parser import text_string_to_metric_families

    counters = [
        m.name
        for m in text_string_to_metric_families(generate_latest().decode())
        if m.type == "counter"
    ]
    assert "test_metrics" in counters
    return "ok"


@svc.api(input=JSON(), output=JSON())
async def echo_json(json_obj: JSONSerializable) -> JSONSerializable:
    batch_ret = await py_model.echo_json.async_run([json_obj])
    return batch_ret[0]


@svc.api(input=JSON(), output=JSON())
def echo_json_sync(json_obj: JSONSerializable) -> JSONSerializable:
    batch_ret = py_model.echo_json.run([json_obj])
    return batch_ret[0]


class ValidateSchema(pydantic.BaseModel):
    name: str
    endpoints: t.List[str]


@svc.api(
    input=JSON(pydantic_model=ValidateSchema),
    output=JSON(),
)
async def echo_json_enforce_structure(json_obj: JSONSerializable) -> JSONSerializable:
    batch_ret = await py_model.echo_json.async_run([json_obj])
    return batch_ret[0]


@svc.api(input=JSON(), output=JSON())
async def echo_obj(obj: JSONSerializable) -> JSONSerializable:
    return await py_model.echo_obj.async_run(obj)


@svc.api(
    input=NumpyNdarray(shape=(2, 2), enforce_shape=True),
    output=NumpyNdarray(shape=(2, 2)),
)
async def predict_ndarray_enforce_shape(inp: NDArray[t.Any]) -> NDArray[t.Any]:
    assert inp.shape == (2, 2)
    return await py_model.predict_ndarray.async_run(inp)


@svc.api(
    input=NumpyNdarray(dtype="uint8", enforce_dtype=True),
    output=NumpyNdarray(dtype="str"),
)
async def predict_ndarray_enforce_dtype(inp: NDArray[t.Any]) -> NDArray[t.Any]:
    assert inp.dtype == np.dtype("uint8")
    return await py_model.predict_ndarray.async_run(inp)


@svc.api(
    input=NumpyNdarray(),
    output=NumpyNdarray(),
)
async def predict_ndarray_multi_output(
    inp: "np.ndarray[t.Any, np.dtype[t.Any]]",
) -> "np.ndarray[t.Any, np.dtype[t.Any]]":
    out1, out2 = await py_model.echo_multi_ndarray.async_run(inp, inp)
    return out1 + out2


@svc.api(
    input=PandasDataFrame(dtype={"col1": "int64"}, orient="records"),
    output=PandasDataFrame(),
)
async def predict_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    assert df["col1"].dtype == "int64"
    output = await py_model.predict_dataframe.async_run(df)
    dfo = pd.DataFrame()
    dfo["col1"] = output
    assert isinstance(dfo, pd.DataFrame)
    return dfo


@svc.api(input=File(), output=File())
async def predict_file(f: FileLike[bytes]) -> bytes:
    batch_ret = await py_model.predict_file.async_run([f])
    return batch_ret[0]


@svc.api(input=Image(), output=Image(mime_type="image/bmp"))
async def echo_image(f: PILImage) -> NDArray[t.Any]:
    assert isinstance(f, PILImage)
    return np.array(f)


@svc.api(
    input=Multipart(original=Image(), compared=Image()),
    output=Multipart(img1=Image(), img2=Image()),
)
async def predict_multi_images(original: Image, compared: Image):
    output_array = await py_model.predict_multi_ndarray.async_run(
        np.array(original), np.array(compared)
    )
    img = fromarray(output_array)
    return dict(img1=img, img2=img)


@svc.api(
    input=Multipart(original=Image(), compared=Image()),
    output=Multipart(img1=Image(), img2=Image()),
)
async def predict_different_args(compared: Image, original: Image):
    output_array = await py_model.predict_multi_ndarray.async_run(
        np.array(original), np.array(compared)
    )
    img = fromarray(output_array)
    return dict(img1=img, img2=img)


@svc.api(
    input=Text(),
    output=Text(),
)
async def use_context(inp: str, ctx: bentoml.Context):
    if "error" in ctx.request.query_params:
        ctx.response.status_code = 400
        return ctx.request.query_params["error"]
    elif "state" in ctx.request.query_params:
        return ctx.state[ctx.request.query_params["state"]]
    return inp


@svc.api(
    input=Text(),
    output=Text(),
)
async def predict_text_stream(inp: str) -> t.AsyncGenerator[str, None]:
    return stream_runner.count_text_stream.async_stream(inp)


@svc.api(
    input=Text(),
    output=Text(),
)
def yo(inp: str) -> str:
    return f"yo {inp}"


# customise the service
class AllowPingMiddleware:
    def __init__(
        self,
        app: ASGIApp,
    ) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            req = Request(scope, receive)
            if req.url.path == "/ping":
                scope["path"] = "/livez"

        await self.app(scope, receive, send)
        return


svc.add_asgi_middleware(AllowPingMiddleware)  # type: ignore (hint not yet supported for hooks)


fastapi_app = FastAPI()


@fastapi_app.get("/hello")
def hello():
    return {"Hello": "World"}


svc.mount_asgi_app(fastapi_app)


def get_uid():
    import uuid

    return str(uuid.uuid4())


@svc.on_deployment
def on_deployment():
    test_dir = os.getenv("BENTOML_TEST_DATA")
    if not test_dir or not os.path.exists(test_dir):
        return
    deployment_file = os.path.join(test_dir, f"deployment-{get_uid()}.txt")
    with open(deployment_file, "w"):
        pass


@svc.on_startup
def on_startup(ctx: bentoml.Context):
    ctx.state["data"] = "hello"
    test_dir = os.getenv("BENTOML_TEST_DATA")
    if not test_dir or not os.path.exists(test_dir):
        return
    text_file = os.path.join(test_dir, f"data-{get_uid()}.txt")
    with open(text_file, "w"):
        pass
    ctx.state["text_file"] = text_file


@svc.on_shutdown
def on_shutdown(ctx: bentoml.Context):
    if "text_file" not in ctx.state:
        return

    with open(ctx.state["text_file"], "a") as f:
        f.write("closed\n")
