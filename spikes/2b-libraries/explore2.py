from harness import *
with Server(fresh_project()) as s:
    k=s.client(120000); s.open_schematic(); s.open_board()
    k.load_all_libraries(); k.wait_for_libraries(interval_ms=100)
    for lid in ["Device:R","Device:C","power:GND","Amplifier_Operational:LM358","MCU_ST_STM32F1:STM32F103C8Tx","Connector_Generic:Conn_01x04"]:
        w=k.get_library_symbol(lid)
        print(lid, "items",len(w.items),"pins",len(w.pins),"units",w.unit_count)
