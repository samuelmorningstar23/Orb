def test_original_catalogue_is_restored_with_predecessor_data_intact():
 from catalogue_probe import STATE,snapshot,persist
 STATE['after']=snapshot()
 assert STATE['after']==STATE['before']
 assert STATE['during']['db_path']!=STATE['before']['db_path']
 persist()
