"""已失败的成交量偏向设计必须继续被冻结复杂度合同拒绝。"""
import unittest
from factor_miner.schema import FactorNode
from factor_miner.dsl import validate_ast
from factor_miner.errors import FactorMinerError


def expression():
    c = dict(op='field', field='close'); v = dict(op='field', field='volume')
    return dict(op='div', args=[dict(op='rolling_sum', window=20, args=[
        dict(op='mul', args=[v, dict(op='sign', args=[dict(op='calendar_delta', args=[c], period=1)])])]),
        dict(op='rolling_sum', window=20, args=[v])])


class SignedVolumeBalanceTest(unittest.TestCase):
    def test_original_six_level_design_is_rejected(self):
        with self.assertRaisesRegex(FactorMinerError, 'AST 深度超过限制'):
            validate_ast(FactorNode.model_validate(expression()), {'close', 'volume'}, ())


if __name__ == '__main__':
    unittest.main()
