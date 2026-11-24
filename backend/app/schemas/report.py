"""问答报表的持久化及前端显示契约。"""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, field_validator, model_validator

MAX_REPORT_ROWS = 249
MAX_REPORT_COLUMNS = 30
MAX_REPORT_TEXT = 32_000
MAX_REPORT_CODE = 12_000
MAX_EXACT_INTEGER = 999_999_999_999_999
MAX_CHART_SERIES = 4

ReportLabel = Annotated[str, Field(min_length=1, max_length=120, pattern=r"^[^\x00-\x1f\x7f]+$")]
ReportCell = (
    Annotated[str, Field(max_length=500, pattern=r"^[^\x00-\x08\x0b\x0c\x0e-\x1f\x7f]*$")]
    | StrictBool
    | StrictInt
    | StrictFloat
    | None
)


class ReportChart(BaseModel):
    """仅允许由前端映射到内置组件的图表参数。"""

    model_config = ConfigDict(extra="forbid")

    title: ReportLabel
    type: Literal["bar", "line"]
    x: ReportLabel
    y: list[ReportLabel] = Field(min_length=1, max_length=MAX_CHART_SERIES)
    unit: ReportLabel | None = None


class ReportData(BaseModel):
    """以同一组汇总行支撑图表、明细和文件导出。"""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    title: ReportLabel
    explanation: str = Field(min_length=1, max_length=2000)
    columns: list[ReportLabel] = Field(min_length=2, max_length=MAX_REPORT_COLUMNS)
    rows: list[list[ReportCell]] = Field(min_length=1, max_length=MAX_REPORT_ROWS)
    charts: list[ReportChart] = Field(min_length=1, max_length=4)

    @field_validator("title", "explanation")
    @classmethod
    def nonblank(cls, value: str) -> str:
        """避免持久化空标题或无法核对的统计说明。"""
        value = value.strip()
        if not value:
            raise ValueError("标题和计算说明不能为空")
        return value

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        """保证图表列有效、数值有限且可跨浏览器和 Excel 保真。"""
        if len(set(self.columns)) != len(self.columns) or any(not column.strip() for column in self.columns):
            raise ValueError("表头必须非空且唯一")
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("数据行列数必须与表头一致")
        cells = [cell for row in self.rows for cell in row if cell is not None]
        if sum(map(len, self.columns)) + sum(len(str(cell)) for cell in cells) > MAX_REPORT_TEXT:
            raise ValueError("报表内容过大，请缩小汇总结果")
        if any(type(cell) is int and abs(cell) > MAX_EXACT_INTEGER for cell in cells):
            raise ValueError("超过十五位的整型编号请写成字符串")
        for chart in self.charts:
            if chart.x not in self.columns or not set(chart.y) <= set(self.columns):
                raise ValueError("图表维度或指标须对应实际列名")
            if chart.x in chart.y or len(set(chart.y)) != len(chart.y):
                raise ValueError("图表维度与指标列须各不相同")
            axis = [row[self.columns.index(chart.x)] for row in self.rows]
            if any(value is None or type(value) is bool or value == "" for value in axis):
                raise ValueError("图表维度需要非空文本或数值")
            for key in chart.y:
                index = self.columns.index(key)
                values = [row[index] for row in self.rows if row[index] is not None]
                if not values or any(type(value) not in (int, float) for value in values):
                    raise ValueError("图表指标列需要数值，缺失值可使用 null")
        return self


class ReportRef(BaseModel):
    """历史和 SSE 仅返回报表标识与标题。"""

    id: UUID
    title: str


class ReportView(ReportData):
    """授权后的报表内容和可审阅计算代码。"""

    id: UUID
    source_files: list[str]
    code: str
