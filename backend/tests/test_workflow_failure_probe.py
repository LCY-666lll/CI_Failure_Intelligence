"""仅用于验证 GitHub Actions 失败处理路径，禁止合并到 main。"""


def test_workflow_failure_probe() -> None:
    """故意产生一次可控失败，触发 CI 的失败报告处理流程。"""

    assert False, "故意触发 GitHub Actions 失败路径"
