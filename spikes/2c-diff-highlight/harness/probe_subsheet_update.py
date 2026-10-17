"""Finding for 2A/implementation: update_items on the UNBOUND Schematic returns the new value but
does not change the document for symbols on a sub-sheet; a sheet-bound Schematic (for_sheet) does."""
import json, subprocess
from common import *

out = {}
for mode in ("unbound", "for_sheet"):
    d = copy_fixture("complex_hierarchy", Path(f"/tmp/c2/h-{mode}"))
    with Server(d) as s:
        sch = s.sch
        vert = next(i for i in sch.get_hierarchy() if i.name == "ampli_ht_vertical")
        target = sch if mode == "unbound" else sch.for_sheet(vert)
        t = next(x for x in target.get_symbols() if x.reference == "R201")
        t.value = "SPIKE"
        ret = target.update_items([t])
        reread = [x.value for x in sch.get_symbols() if x.reference == "R201"]
        sch.save()
        on_disk = subprocess.run("grep -c SPIKE ampli_ht.kicad_sch", shell=True, cwd=d, capture_output=True, text=True).stdout.strip()
    out[mode] = dict(update_items_returned=[x.value for x in ret], reread_value=reread, spike_lines_in_ampli_ht_on_disk=on_disk)
    print(mode, out[mode])
json.dump(out, open("/w/spikes/2c-diff-highlight/results/probe_subsheet_update.json", "w"), indent=1)
