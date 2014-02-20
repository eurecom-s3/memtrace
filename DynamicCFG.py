from TraceEntries import *
from BasicBlocks import *
from pygraph.classes.exceptions import AdditionError
import sys

def build_dynamic_cfg(trace_file, basic_blocks_path):
    all_bbs = []
    bbs_start_pos = {}
    for bb in get_basic_blocks(basic_blocks_path):
        all_bbs += [bb]
        bbs_start_pos[bb.start] = bb
    graph = \
            build_static_cfg(
                    get_outgoing(all_bbs))
    print("static analysis done, bbs=%d" % len(all_bbs))
    total = 0
    last_bb = None
    edges = {}
    added_edges = 0
    for h, e in trace_file.generate_elements():
        # this should be ExecutionTraceInstr, but we dont have this data
        # another experiment is needed
        #if e.__class__ == ExecutionTraceInstr:
        if e.__class__ == ExecutionTraceMemory:
            total += 1
        try:
            try:
                this_bb = bbs_start_pos[e._data['pc']]
            except:
                # not a start
                continue
            if last_bb is None:
                last_bb = this_bb
                continue
            try:
                # add edge from last_bb to this_bb, only if doesn't exist
                if str(this_bb) not in edges[str(last_bb)]:
                    edges[str(last_bb)].append(str(this_bb))
                    try:
                        graph.add_edge((last_bb, this_bb))
                        added_edges += 1
                    except AdditionError:
                        pass
            except KeyError:
                edges[str(last_bb)] = [str(this_bb)]
                pass
            this_bb = last_bb
        except KeyError:
            continue
    print("total = %d, added_edges = %d" % (total, added_edges))
    return graph

if __name__ == "__main__":
    build_dynamic_cfg(TraceFile(sys.argv[1]), sys.argv[2])