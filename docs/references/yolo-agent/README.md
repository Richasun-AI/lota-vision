# YOLO-Agent 参考资料

从 `SI4Science/YoloInspectionAgent/YOLO-Agent`（MIT 许可，见 LICENSE）复制过来的、被设计文档引用的 3 个文件，只作研究参考，平台代码不依赖它们。

- `training-modes.md`：训练档位
- `custom-dataset.md`：自定义数据集指南
- `active_learning.py`：难例挖掘（主动学习）实现

> 这些文件里还有指向 YOLO-Agent 其他文档的链接（cli.md、quickstart.md 等），那些文档没有复制过来，链接打不开；需要时去 YOLO-Agent 原仓库看。

**原仓库**：https://github.com/whut09/YOLO-Agent （本机原来那份是它的完整克隆，2026-11-12 核对：没有本地改动，和 GitHub 最新提交 `f22ec31`（2026-10-16）一致，可以随时重新 clone）

**以后可能值得回头看的部分**（做“训练效果不好，自动找原因、自动调参”时）：
- `docs/data-optimization.md`、`docs/evidence.md`：先按 COCO 错误类型（误检、漏检、定位不准、小目标）诊断，再决定改数据还是改训练
- `docs/automatic-optimization-architecture.md`：小规模试训 + ASHA 提前淘汰差的方案，控制 GPU 预算
- `docs/sahi-inference-certification.md`：切片推理，提升大图里小目标的检出（如远处的空开、标签）
