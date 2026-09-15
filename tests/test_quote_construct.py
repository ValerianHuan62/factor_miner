"""直接报价测量的量纲、方向和反例，不检验未来收益。"""
from factor_miner.construct_validation import ObservableCondition, validate_construct
from factor_miner.schema import FactorNode


def f(name):
    return FactorNode(op='field',field=name)


def test_quoted_width_and_trade_position_are_distinct_measurements():
    bid,ask,last=f('quote_bid_raw'),f('quote_ask_raw'),f('quote_close_raw')
    total=FactorNode(op='add',args=(bid,ask))
    width=FactorNode(op='div',args=(FactorNode(op='sub',args=(ask,bid)),total))
    location=FactorNode(op='div',args=(last,FactorNode(op='mul',args=(total,FactorNode(op='const',value=.5)))))
    for node,test in [(width,'quoted_spread_width'),(location,'quote_trade_location')]:
        condition=ObservableCondition(observation='直接报价测量',measurement=test,response_test=test)
        assert validate_construct(node,condition)['status']=='基础检验符合'
        assert validate_construct(node,condition.model_copy(update={'expected_response':'decrease'}))['status']!='基础检验符合'
    # 扩报价不能被偷换为“最后成交价上移”，反之亦然。
    assert validate_construct(location,ObservableCondition(observation='价差',measurement='价差',response_test='quoted_spread_width'))['status']!='基础检验符合'
    assert validate_construct(width,ObservableCondition(observation='位置',measurement='位置',response_test='quote_trade_location'))['status']!='基础检验符合'
