import re
from pygraph.classes.digraph import digraph
from pygraph.classes.digraph import AdditionError
from pygraph.readwrite.dot import write
import collections
import logging

STATE_BEFORE_BEGINNING = 0
STATE_INSIDE = 1
STATE_AFTER = 2

CF_REGULAR = 0
CF_UNCONDITIONAL_BRANCH = 1
CF_CONDITIONAL_BRANCH = 2
CF_INDIRECT = 3
CF_CALL = 4
CF_INDIRECT_CALL = 5
CF_RETURN = 6

RE_HEXNUM = re.compile("(0x)?[0-9a-fA-F]+")

log = logging.getLogger(__name__)
class BasicBlock():
    def __init__(self, start, end = 0, last_line = "", translated = False):
        self.start = start
        self.end = end
        self.last_line = last_line
        self.control_flow = []
        self.exit_flags = []
        self.is_translated = translated
        self.successors = []
        
    def __str__(self):
        if self.is_translated:
            return "0x%08x-0x%08x" % (self.start, self.end)
        else:
            return "0x%08x-" % self.start
        
    def __repr__(self):
        return self.__str__()
        
class Function():
    def __init__(self, name, head_bb):
        self.name = name
        self.head = head_bb
        
    def get_basic_blocks(self):
        bbs = []
        bbs_todo = [self.head]
        
        while bbs_todo:
            bb = bbs_todo.pop()
            bbs.append(bb)
            for successor in bb.successors:
                if not (successor in bbs or successor in bbs_todo):
                    bbs_todo.append(successor)
                    
        return bbs
        
    def get_edges(self):
        edges = []
        bbs_todo = [self.head]
        bbs_done = []
        
        while bbs_todo:
            bb = bbs_todo.pop()
            bbs_done.append(bb)
            for successor in bb.successors:
                if not successor in bbs_done and not successor in bbs_todo:
                    bbs_todo.append(successor)
                edges.append((bb, successor))
        return edges
        
        
    def __str__(self):
        return self.name
        
    def __repr__(self):
        return self.__str__()
        
def get_basic_blocks(qemu_trace_file):
    """Takes the path to a qemu trace file (with in_asm tracing enabled) 
       and outputs an iterator of BasicBlock objects."""
    RE_PC = re.compile("^(0x[0-9a-f]{8}):.*$")
    
    with open(qemu_trace_file, 'r') as file:
        state = STATE_AFTER
        lastline = None
        for line in file.readlines():
            line = line.strip()
            match = RE_PC.match(line)
        
            if line.startswith("IN:"):
                state = STATE_BEFORE_BEGINNING
            elif state == STATE_BEFORE_BEGINNING and match:
                start_pc = int(match.group(1), 16)
                end_pc = start_pc
                lastline = line
                state = STATE_INSIDE
            elif state == STATE_INSIDE:
                if match:
                    end_pc = int(match.group(1), 16)
                    lastline = line
                else:
                    yield BasicBlock(start_pc, end_pc, lastline, True)
                    state = STATE_AFTER
                    
def find_exit_conditions(opcode):
    """Take an ARM opcode (32 bit number), extract the condition field and return
       an array of (flag, value) tuples. Values can be 0, 1 or 'S' for symbolic.
       If the flags are set accordingly, two states should be spawned, were one
       executes the conditional instruction and the other doesn't."""
    condition_code = (opcode >> 28) & 0xf
    if condition_code in [0, 1]: #EQ, NE
        return [('Z', 'S')]
    elif condition_code in [2, 3]: #CS, CC
        return [('C', 'S')]
    elif condition_code in [4, 5]: #MI, PL
        return [('N', 'S')]
    elif condition_code in [6, 7]: #VS, VC
        return [('V', 'S')]
    elif condition_code in [8, 9]: #HI, LS
        return [('C', 1), ('Z', 'S')]
    elif condition_code in [10, 11]: #GE/LT
        return [('N', 1), ('V', 'S')]
    elif condition_code == 12: #GT
        return [('Z', 0), ('N', 1), ('V', 'S')]
    elif condition_code == 13: #LE
        return [('Z', 1), ('N', 1), ('V', 'S')]
    else:
        return []
    
    
                        
def parse_mnem(pc, mnem, params):
    """Parses an ARM mnemonic and returns the control flow for this instruction.
       The control flow is a tuple of (Control flow type, static target PC)."""
    if mnem == "b":
        match = RE_HEXNUM.match(params.strip())
        if match:
            return [(CF_UNCONDITIONAL_BRANCH, int(match.group(0), 16))]
        else:
            raise RuntimeError("Cannot parse branch target of direct branch")
    elif mnem == "bl":
        match = RE_HEXNUM.match(params.strip())
        if match:
            return [(CF_CALL, int(match.group(0), 16)), (CF_REGULAR, pc + 4)]
        else:
            return [(CF_INDIRECT_CALL, None), (CF_REGULAR, pc + 4)]  
    elif mnem == "blx":
        raise RuntimeError("BLX instruction encountered, no code here to handle thumb") 
    elif mnem.startswith("b"):
        match = RE_HEXNUM.match(params.strip())
        if match:
            return [(CF_REGULAR, pc + 4), (CF_CONDITIONAL_BRANCH, int(match.group(0), 16))]
        else:
            raise RuntimeError("Cannot parse branch target of conditional branch")
    elif mnem == "mov" and params.startswith("pc"):
        return [(CF_RETURN, None)]  
    elif mnem.startswith("mov") and params.startswith("pc"):
        return [(CF_RETURN, None), (CF_REGULAR, pc + 4)]  
    elif mnem == "ldr" and params.startswith("pc, [pc"):
        log.warning("TODO: figure out target of LDR at 0x%08x" % pc)
        return [(CF_UNCONDITIONAL_BRANCH, None)]
    elif mnem == "ldr":
        return [(CF_INDIRECT, None)]
    elif mnem == "pop" and "pc" in params:
        return [(CF_RETURN, None)]
    elif mnem.startswith("pop") and "pc" in params:
        return [(CF_RETURN, None), (CF_REGULAR, pc + 4)]
    else:
        log.warning("WARNING: Unknown instruction '%s' '%s' at 0x%x" % (mnem, params, pc))
        return [(CF_REGULAR, pc + 4)]
                        
def get_outgoing(basic_blocks):
    """Enriches an iterator of basic blocks so that the control_flow attribute of each
       basic block is meaningful."""
    RE_INSTR = re.compile("^(0x[0-9a-f]{8}):\s+([0-9a-f]{8})\s+([a-z]+)\s+([^;]*)(;.*)?$")
    for bb in basic_blocks:
        match = RE_INSTR.match(bb.last_line)
        if match:
            pc = int(match.group(1), 16)
            opcode = int(match.group(2), 16)
            mnem = match.group(3)
            params = match.group(4)

            bb.control_flow = parse_mnem(pc, mnem, params)
            bb.exit_flags = find_exit_conditions(opcode)
            yield bb  
            
def group_functions(basic_blocks):
    functions = {}
    
    nodes = {}
    is_first_bb = True
    for bb in get_outgoing(basic_blocks):
        if is_first_bb:
            is_first_bb = False
            functions[bb.start] = Function("entry_0x%08x" % bb.start, bb)
        nodes[bb.start] = bb
        for cf in bb.control_flow:
            if cf[0] in [CF_REGULAR, CF_CONDITIONAL_BRANCH, CF_UNCONDITIONAL_BRANCH, CF_INDIRECT] and not cf[1] is None:
                if not cf[1] in nodes:
                    nodes[cf[1]] = BasicBlock(cf[1])
            elif cf[0] in [CF_CALL] and not cf[1] is None:
                functions[cf[1]] = Function("func_0x%08x" % cf[1], None)
                
    
    successors = collections.defaultdict(list)
    for node in nodes.values():
        if node.start in functions:
            functions[node.start].head = node
            
        for cf in node.control_flow:
            if cf[0] in [CF_REGULAR, CF_CONDITIONAL_BRANCH, CF_UNCONDITIONAL_BRANCH, CF_INDIRECT] and not cf[1] is None:
                node.successors.append(nodes[cf[1]])
                    
    return functions
    
def graph_functions(functions):
    for function in functions.values():
        function_graph = digraph()
        
        for node in function.get_basic_blocks():
            if node.is_translated:
                function_graph.add_node(node)
            else:
                function_graph.add_node(node, attrs = [("style", "filled"), ("fillcolor", "#A0A0A0")])
        
        for edge in function.get_edges():
            function_graph.add_edge(edge)
            
        yield(function, function_graph)
        
def build_function_call_graph(functions):
    edges = []
    graph = digraph()
    
    graph.add_nodes(functions.values()) 
    for function in functions.values():
        for bb in function.get_basic_blocks():
            for cf in bb.control_flow:
                if cf[0] in [CF_CALL] and not cf[1] is None:
                    edge = (function, functions[cf[1]])
                    if not edge in edges:
                        edges.append(edge)
                        try:
                            graph.add_edge(edge)
                        except AdditionError as err:
                            pass
        
    return graph
                
def build_static_cfg(basic_blocks, no_function_inlining = True, add_unexplored_bbs = True):
    """Build a pygraph digraph object from an iterator of basic blocks."""
    nodes = {}
    edges = []
    unexplored_bbs = []
    for bb in get_outgoing(basic_blocks):
         nodes[bb.start] = bb
         for cf in bb.control_flow:
             if not cf[1] is None:
                 if no_function_inlining and cf[0] in [CF_REGULAR, CF_CONDITIONAL_BRANCH, CF_UNCONDITIONAL_BRANCH, CF_INDIRECT]:
                     edges.append((bb.start, cf[1]))
                 elif not no_function_inlining:
                     edges.append((bb.start, cf[1]))
                 
    if add_unexplored_bbs:
        for bb in nodes.values():
            for cf in bb.control_flow:
                if not cf[1] is None and not nodes.has_key(cf[1]):
                    nodes[cf[1]] = BasicBlock(cf[1])
                    unexplored_bbs += [nodes[cf[1]]]
        
    graph = digraph()
    for node in nodes.values():
        if node.is_translated:
            graph.add_node(node)
        else:
            graph.add_node(node, attrs = [("style", "filled"), ("fillcolor", "#A0A0A0")])
            
    for edge in edges:
        try:
            start = nodes[edge[0]]
            end = nodes[edge[1]]
            log.debug("Start: %s, End: %s" % (repr(start), repr(end)))
            try:
                graph.add_edge((start, end))
            except AdditionError:
                log.warning("Edge already in graph, not adding")
        except KeyError as err:
            log.warning("Dropping edge 0x%08x-0x%08x because one node seems to be not in the graph" % (edge[0], edge[1]))
        
    return (graph, unexplored_bbs)
                    
if __name__ == "__main__":
    import sys
    import os
    import errno
#    for bb in BasicBlocks(sys.argv[1]).get_basic_blocks():
#        print("Basic block: 0x%08x - 0x%08x" % (bb[0], bb[1]))
#    for bb in get_outgoing(get_basic_blocks(sys.argv[1])):
#        print("BB 0x%08x-0x%08x targets %s" % (bb.start, bb.end, repr(bb.control_flow)))

    try:
        os.makedirs("functions")
    except OSError as err:
        if not (err.errno == errno.EEXIST):
            raise err

    functions = group_functions(get_basic_blocks(sys.argv[1]))
    for function_graph in graph_functions(functions):
        dot = write(function_graph[1])
        with open(os.path.join("functions", function_graph[0].name + ".dot"), 'w') as file:
            file.write(dot)
            
    dot = write(build_function_call_graph(functions))
    with open("function_call_graph.dot", 'w') as file:
        file.write(dot)
            
    
    
    
    
                
                
