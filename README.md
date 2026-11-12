# 气候贷款的重置现金流与赠款等值比较

本项目保存气候行动研究的合成资料与领域口径。`analysis_data.io`读取JSON及核验原始文件身份，`analysis_data.domain`核对基本形状，`analysis_data.cashflow`做逐段计息、赠款等值与组合汇总，`analysis_data.cli`展示数量与发布报告。详细量纲、时间、契约和统计口径见[资料说明](docs/data-contract.md)。地区、家庭、合同、价格及排放数据均为合成，不代表真实政策评价或投资建议。

Python 3.11以上，无第三方依赖。项目根目录可运行：

```sh
python -m unittest discover -s tests -v
python -m compileall -q analysis_data tests
python -m analysis_data.cli inspect data/normal.json
python -m analysis_data.cli analyze data/month-end-30e.json
```

`inspect`只展示资料，不做拟合、求解或分析发布。`analyze`先逐情景演算合同（同日按结息→放款→费用→还本顺序，事件即切段），再用各币种折现因子与即期换算曲线把现金流换报告币并折现，输出：

- 每个工具的逐段本金/利息台账、现金与资本化事件、赠款等值（含负等值）与百分比；
- 本金、利息、费用三条恒等式，每个差额都能回到具体现金事件；
- 借款人每年本息与费用（现金口径，不把赠款等值当现金或豁免）；
- 按国家与资金渠道的分组，渠道按冻结份额分配并附加回校验；
- 情景权重下的均值与离散分位（先逐情景演算，不先平均利率）；
- 绑定方法版本与清单指纹的`binding`，曲线或合同改动会改指纹，须新建结果；`cli verify <清单> --report <报告>`复核绑定。

遇到缺报价/曲线越界、还本超过余额、同id多份合同、日历不完整等情况，`analyze`输出`status:"blocked"`并以非零码退出，错误定位到工具与日期，阻止该组合发布。

资料：`normal.json`为基础赠款与浮息贷款；`leap-year-capitalization.json`覆盖闰年实际天数、分批放款与利息资本化后还本；`month-end-30e.json`覆盖月末30E/360（28/32天段）、外币fx、现付费用与三档融资成本情景；`capitalized-fee.json`覆盖费用资本化后还本；`missing-curve-date.json`、`over-repayment.json`、`missing-rate-quote.json`为应被拦截的资料；`invalid-config.json`包含基本配置错误。有理数以字符串保留，null不等于0，随机种子属于分析设计。

Dockerfile固定Linux镜像；`docker build -t climate-analysis .`构建，`docker run --rm climate-analysis`显示摘要，`docker run --rm climate-analysis python -m unittest discover -s tests -v`运行已有读取检查。资料均可本地读取，运行不需要线上账户。
