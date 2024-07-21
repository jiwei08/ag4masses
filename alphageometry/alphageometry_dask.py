# Copyright 2023 DeepMind Technologies Limited
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""Run DD+AR or AlphaGeometry solver.

Please refer to README.md for detailed instructions.
"""

import time
import traceback

from absl import app
from absl import flags
from absl import logging
import ddar
import graph as gh
import lm_inference as lm
import pretty as pt
import problem as pr

#=============
import sys, os, math, re
import multiprocessing
from dask.distributed import Client, LocalCluster, progress, wait
model = None # global variable used in multi-processing workers

_GIN_SEARCH_PATHS = flags.DEFINE_list(
    'gin_search_paths',
    ['third_party/py/meliad/transformer/configs'],
    'List of paths where the Gin config files are located.',
)
_GIN_FILE = flags.DEFINE_multi_string(
    'gin_file', ['base_htrans.gin'], 'List of Gin config files.'
)
_GIN_PARAM = flags.DEFINE_multi_string(
    'gin_param', None, 'Newline separated list of Gin parameter bindings.'
)

_PROBLEMS_FILE = flags.DEFINE_string(
    'problems_file',
    'imo_ag_30.txt',
    'text file contains the problem strings. See imo_ag_30.txt for example.',
)
_PROBLEM_NAME = flags.DEFINE_string(
    'problem_name',
    'imo_2000_p1',
    'name of the problem to solve, must be in the problem_file.',
)
_MODE = flags.DEFINE_string(
    'mode', 'ddar', 'either `ddar` (DD+AR) or `alphageometry`')
_DEFS_FILE = flags.DEFINE_string(
    'defs_file',
    'defs.txt',
    'definitions of available constructions to state a problem.',
)
_RULES_FILE = flags.DEFINE_string(
    'rules_file', 'rules.txt', 'list of deduction rules used by DD.'
)
_CKPT_PATH = flags.DEFINE_string('ckpt_path', '', 'checkpoint of the LM model.')
_VOCAB_PATH = flags.DEFINE_string(
    'vocab_path', '', 'path to the LM vocab file.'
)
_OUT_FILE = flags.DEFINE_string(
    'out_file', '', 'path to the solution output file.'
)  # pylint: disable=line-too-long
_BEAM_SIZE = flags.DEFINE_integer(
    'beam_size', 1, 'beam size of the proof search.'
)  # pylint: disable=line-too-long
_SEARCH_DEPTH = flags.DEFINE_integer(
    'search_depth', 1, 'search depth of the proof search.'
)  # pylint: disable=line-too-long

#===================================
_N_WORKSERS = flags.DEFINE_integer(
    'n_workers', 1, 'number of workers'
)# pylint: disable=line-too-long

DEFINITIONS = None  # contains definitions of construction actions
RULES = None  # contains rules of deductions


def natural_language_statement(logical_statement: pr.Dependency) -> str:
  """Convert logical_statement to natural language.

  Args:
    logical_statement: pr.Dependency with .name and .args

  Returns:
    a string of (pseudo) natural language of the predicate for human reader.
  """
  names = [a.name.upper() for a in logical_statement.args]
  names = [(n[0] + '_' + n[1:]) if len(n) > 1 else n for n in names]
  return pt.pretty_nl(logical_statement.name, names)


def proof_step_string(
    proof_step: pr.Dependency, refs: dict[tuple[str, ...], int], last_step: bool
) -> str:
  """Translate proof to natural language.

  Args:
    proof_step: pr.Dependency with .name and .args
    refs: dict(hash: int) to keep track of derived predicates
    last_step: boolean to keep track whether this is the last step.

  Returns:
    a string of (pseudo) natural language of the proof step for human reader.
  """
  premises, [conclusion] = proof_step

  premises_nl = ' & '.join(
      [
          natural_language_statement(p) + ' [{:02}]'.format(refs[p.hashed()])
          for p in premises
      ]
  )

  if not premises:
    premises_nl = 'similarly'

  refs[conclusion.hashed()] = len(refs)

  conclusion_nl = natural_language_statement(conclusion)
  if not last_step:
    conclusion_nl += ' [{:02}]'.format(refs[conclusion.hashed()])

  return f'{premises_nl} \u21d2 {conclusion_nl}'


def write_solution(g: gh.Graph, p: pr.Problem, out_file: str) -> None:
  """Output the solution to out_file.

  Args:
    g: gh.Graph object, containing the proof state.
    p: pr.Problem object, containing the theorem.
    out_file: file to write to, empty string to skip writing to file.
  """
  setup, aux, proof_steps, refs = ddar.get_proof_steps(
      g, p.goal, merge_trivials=False
  )

  solution = '\n=========================='
  solution += '\n * From theorem premises:\n'
  premises_nl = []
  for premises, [points] in setup:
    solution += ' '.join([p.name.upper() for p in points]) + ' '
    if not premises:
      continue
    premises_nl += [
        natural_language_statement(p) + ' [{:02}]'.format(refs[p.hashed()])
        for p in premises
    ]
  solution += ': Points\n' + '\n'.join(premises_nl)

  solution += '\n\n * Auxiliary Constructions:\n'
  aux_premises_nl = []
  for premises, [points] in aux:
    solution += ' '.join([p.name.upper() for p in points]) + ' '
    aux_premises_nl += [
        natural_language_statement(p) + ' [{:02}]'.format(refs[p.hashed()])
        for p in premises
    ]
  solution += ': Points\n' + '\n'.join(aux_premises_nl)

  # some special case where the deduction rule has a well known name.
  r2name = {
      'r32': '(SSS)',
      'r33': '(SAS)',
      'r34': '(Similar Triangles)',
      'r35': '(Similar Triangles)',
      'r36': '(ASA)',
      'r37': '(ASA)',
      'r38': '(Similar Triangles)',
      'r39': '(Similar Triangles)',
      'r40': '(Congruent Triangles)',
      'a00': '(Distance chase)',
      'a01': '(Ratio chase)',
      'a02': '(Angle chase)',
  }

  solution += '\n\n * Proof steps:\n'
  for i, step in enumerate(proof_steps):
    _, [con] = step
    nl = proof_step_string(step, refs, last_step=i == len(proof_steps) - 1)
    rule_name = r2name.get(con.rule_name, '')
    nl = nl.replace('\u21d2', f'{rule_name}\u21d2 ')
    solution += '{:03}. '.format(i + 1) + nl + '\n'

  solution += '==========================\n'
  logging.info(solution)
  if out_file:
    with open(out_file, 'w') as f:
      f.write(solution)
    logging.info('Solution written to %s.', out_file)


def get_lm(ckpt_init: str, vocab_path: str) -> lm.LanguageModelInference:
  lm.parse_gin_configuration(
      _GIN_FILE.value, _GIN_PARAM.value, gin_paths=_GIN_SEARCH_PATHS.value
  )

  return lm.LanguageModelInference(vocab_path, ckpt_init, mode='beam_search')


def run_ddar(g: gh.Graph, p: pr.Problem, out_file: str) -> bool:
  """Run DD+AR.

  Args:
    g: gh.Graph object, containing the proof state.
    p: pr.Problem object, containing the problem statement.
    out_file: path to output file if solution is found.

  Returns:
    Boolean, whether DD+AR finishes successfully.
  """
  ddar.solve(g, RULES, p, max_level=1000)

  goal_args = g.names2nodes(p.goal.args)
  if not g.check(p.goal.name, goal_args):
    logging.info('DD+AR failed to solve the problem.')
    return False

  write_solution(g, p, out_file)

  gh.nm.draw(
      g.type2nodes[gh.Point],
      g.type2nodes[gh.Line],
      g.type2nodes[gh.Circle],
      g.type2nodes[gh.Segment])
  return True


def translate_constrained_to_constructive(
    point: str, name: str, args: list[str]
) -> tuple[str, list[str]]:
  """Translate a predicate from constraint-based to construction-based.

  Args:
    point: str: name of the new point
    name: str: name of the predicate, e.g., perp, para, etc.
    args: list[str]: list of predicate args.

  Returns:
    (name, args): translated to constructive predicate.
  """
  if name in ['T', 'perp']:
    a, b, c, d = args
    if point in [c, d]:
      a, b, c, d = c, d, a, b
    if point == b:
      a, b = b, a
    if point == d:
      c, d = d, c
    if a == c and a == point:
      return 'on_dia', [a, b, d]
    return 'on_tline', [a, b, c, d]

  elif name in ['P', 'para']:
    a, b, c, d = args
    if point in [c, d]:
      a, b, c, d = c, d, a, b
    if point == b:
      a, b = b, a
    return 'on_pline', [a, b, c, d]

  elif name in ['D', 'cong']:
    a, b, c, d = args
    if point in [c, d]:
      a, b, c, d = c, d, a, b
    if point == b:
      a, b = b, a
    if point == d:
      c, d = d, c
    if a == c and a == point:
      return 'on_bline', [a, b, d]
    if b in [c, d]:
      if b == d:
        c, d = d, c  # pylint: disable=unused-variable
      return 'on_circle', [a, b, d]
    return 'eqdistance', [a, b, c, d]

  elif name in ['C', 'coll']:
    a, b, c = args
    if point == b:
      a, b = b, a
    if point == c:
      a, b, c = c, a, b
    return 'on_line', [a, b, c]

  elif name in ['^', 'eqangle']:
    a, b, c, d, e, f = args

    if point in [d, e, f]:
      a, b, c, d, e, f = d, e, f, a, b, c

    x, b, y, c, d = b, c, e, d, f
    if point == b:
      a, b, c, d = b, a, d, c

    if point == d and x == y:  # x p x b = x c x p
      return 'angle_bisector', [point, b, x, c]

    if point == x:
      return 'eqangle3', [x, a, b, y, c, d]

    return 'on_aline', [a, x, b, c, y, d]

  elif name in ['cyclic', 'O']:
    a, b, c = [x for x in args if x != point]
    return 'on_circum', [point, a, b, c]

  return name, args


def check_valid_args(name: str, args: list[str]) -> bool:
  """Check whether a predicate is grammarically correct.

  Args:
    name: str: name of the predicate
    args: list[str]: args of the predicate

  Returns:
    bool: whether the predicate arg count is valid.
  """
  if name == 'perp':
    if len(args) != 4:
      return False
    a, b, c, d = args
    if len({a, b}) < 2:
      return False
    if len({c, d}) < 2:
      return False
  elif name == 'para':
    if len(args) != 4:
      return False
    a, b, c, d = args
    if len({a, b, c, d}) < 4:
      return False
  elif name == 'cong':
    if len(args) != 4:
      return False
    a, b, c, d = args
    if len({a, b}) < 2:
      return False
    if len({c, d}) < 2:
      return False
  elif name == 'coll':
    if len(args) != 3:
      return False
    a, b, c = args
    if len({a, b, c}) < 3:
      return False
  elif name == 'cyclic':
    if len(args) != 4:
      return False
    a, b, c, d = args
    if len({a, b, c, d}) < 4:
      return False
  elif name == 'eqangle':
    if len(args) != 8:
      return False
    a, b, c, d, e, f, g, h = args
    if len({a, b, c, d}) < 3:
      return False
    if len({e, f, g, h}) < 3:
      return False
  return True


def try_translate_constrained_to_construct(string: str, g: gh.Graph) -> str:
  """Whether a string of aux construction can be constructed.

  Args:
    string: str: the string describing aux construction.
    g: gh.Graph: the current proof state.

  Returns:
    str: whether this construction is valid. If not, starts with "ERROR:".
  """
  if string[-1] != ';':
    return 'ERROR: must end with ;'

  logging.info(f'PID={os.getpid()}: !! try_translate_constrained_to_construct: string=%s', string)

  # sometimes the LM may return ill-formed result with multiple colons.
  # example:
  #
  # napoleon2
  # a1 a2 a3 = triangle; c3 = s_angle a1 a2 c3 30, s_angle a2 a1 c3 150; c1 = s_angle a2 a3 c1 30, s_angle a3 a2 c1 150; c2 = s_angle a3 a1 c2 30, s_angle a1 a3 c2 150 ? cong c1 c2 c1 c3
  #
  # in the process, 
  # I0210 17:58:01.513668 140016515833856 alphageometry.py:550] Decoding from {S} a : ; b : ; c : ; d : ^ a d a b 5. pi / 6. 00 ^ b d b a 1. pi / 6. 01 ; e : ^ b e b c 5. pi / 6. 02 ^ c e c b 1. pi / 6. 03 ; f : ^ a f a c 1. pi / 6. 04 ^ c f c a 5. pi / 6. 05 ? D e f e d {F1} x00 g : C a b g 06 D a g b g 07 ; x00 h : C c b h 08 D c h b h 09 ; x00
  # I0210 18:01:38.182158 140016515833856 alphageometry.py:384] !! try_translate_constrained_to_construct: string=i : C a c i 10 D a i c i 11 ? V d f {F1} x00 j : D g j h j 12 D h j i j 13 ;

  #XXX
  # str_parts = string.split(' : ')
  # if len(str_parts) != 2:
  #   return f'ERROR: string has multiple colons: |{string}|'
  mch = re.match('(.*?)( \? | \. \{)', string)
  if mch :
    strFixed = mch.group(1) + ';'
    logging.info(f'ID={os.getpid()}: Bad LM output: {string}. Changed to {strFixed}')
    string = strFixed

  # sometimes the constraint in string is empty:
  # 0407 17:11:35.470240 126383800963072 alphageometry.py:394] !! try_translate_constrained_to_construct: string=j : ;
  hdprem = string.split(' : ')
  if len(hdprem) !=2 or hdprem[1].strip()==';' :
    logging.info(f'ID={os.getpid()}: Bad LM output: {string}. ERROR')
    return f'ERROR: Bad LM output: {string}'
  head, prem_str = hdprem
  point = head.strip()

  if len(point) != 1 or point == ' ':
    return f'ERROR: invalid point name {point}'

  existing_points = [p.name for p in g.all_points()]
  if point in existing_points:
    return f'ERROR: point {point} already exists.'

  prem_toks = prem_str.split()[:-1]  # remove the EOS ' ;'
  prems = [[]]

  for i, tok in enumerate(prem_toks):
    if tok.isdigit():
      if i < len(prem_toks) - 1:
        prems.append([])
    else:
      prems[-1].append(tok)

  if len(prems) > 2:
    return 'ERROR: there cannot be more than two predicates.'

  clause_txt = point + ' = '
  constructions = []

  for prem in prems:
    name, *args = prem

    if point not in args:
      return f'ERROR: {point} not found in predicate args.'

    if not check_valid_args(pt.map_symbol(name), args):
      return 'ERROR: Invalid predicate ' + name + ' ' + ' '.join(args)

    for a in args:
      if a != point and a not in existing_points:
        return f'ERROR: point {a} does not exist.'

    try:
      name, args = translate_constrained_to_constructive(point, name, args)
    except:  # pylint: disable=bare-except
      return 'ERROR: Invalid predicate ' + name + ' ' + ' '.join(args)

    if name == 'on_aline':
      if args.count(point) > 1:
        return f'ERROR: on_aline involves twice {point}'

    constructions += [name + ' ' + ' '.join(args)]

  clause_txt += ', '.join(constructions)
  clause = pr.Clause.from_txt(clause_txt)

  try:
    g.copy().add_clause(clause, 0, DEFINITIONS)
  except:  # pylint: disable=bare-except
    return 'ERROR: ' + traceback.format_exc()

  return clause_txt


def insert_aux_to_premise(pstring: str, auxstring: str) -> str:
  """Insert auxiliary constructs from proof to premise.

  Args:
    pstring: str: describing the problem to solve.
    auxstring: str: describing the auxiliar construction.

  Returns:
    str: new pstring with auxstring inserted before the conclusion.
  """
  setup, goal = pstring.split(' ? ')
  return setup + '; ' + auxstring + ' ? ' + goal


class BeamQueue:
  """Keep only the top k objects according to their values."""

  def __init__(self, max_size: int = 512):
    self.queue = []
    self.max_size = max_size

  def add(self, node: object, val: float) -> None:
    """Add a new node to this queue."""

    if len(self.queue) < self.max_size:
      self.queue.append((val, node))
      return

    # Find the minimum node:
    min_idx, (min_val, _) = min(enumerate(self.queue), key=lambda x: x[1])

    # replace it if the new node has higher value.
    if val > min_val:
      self.queue[min_idx] = (val, node)

  def __iter__(self):
    for val, node in self.queue:
      yield val, node

  def __len__(self) -> int:
    return len(self.queue)

#XXX
def bqsearch_init(param):
    global model
    logging.info('Worker initializing. PID=%d', os.getpid())
    def get_lm(ckpt_init: str, vocab_path: str) -> lm.LanguageModelInference:
      lm.parse_gin_configuration(
          param['_GIN_FILE'], param['_GIN_PARAM'], gin_paths=param['_GIN_SEARCH_PATHS']
      )

      return lm.LanguageModelInference(vocab_path, ckpt_init, mode='beam_search')
    model = get_lm(param['_CKPT_PATH'], param['_VOCAB_PATH'])

def bqsearch(i_nd, srch_inputs, out_file) -> tuple[int, bool, list]: # ( iNode, solved, [ (node, score) ] )
    pid = os.getpid()
    logging.info(f'Worker PID={pid} called for beam search node {i_nd}')

    prev_score, (g, string, pstring) = srch_inputs
    logging.info(f'Worker PID={pid}: Decoding from {string}')
    outputs = model.beam_decode(string, eos_tokens=[';'])

    # translate lm output to the constructive language.
    # so that we can update the graph representing proof states:
    translations = [
        try_translate_constrained_to_construct(o, g)
        for o in outputs['seqs_str']
    ]

    # couple the lm outputs with its translations
    candidates = zip(outputs['seqs_str'], translations, outputs['scores'])

    # bring the highest scoring candidate first
    candidates = reversed(list(candidates))

    ret = []
    for lm_out, translation, score in candidates:
      logging.info(f'Worker PID={pid}: LM output (score={score}): "{lm_out}"')
      logging.info(f'Worker PID={pid}: Translation: "{translation}"')

      if translation.startswith('ERROR:'):
        # the construction is invalid.
        continue

      # Update the constructive statement of the problem with the aux point:
      candidate_pstring = insert_aux_to_premise(pstring, translation)

      #XXX
      logging.info(f'Worker PID={pid}: string=|{string}| lm_out=|{lm_out}|')
      logging.info(f'Worker PID={pid}: Solving: "{candidate_pstring}"')
      p_new = pr.Problem.from_txt(candidate_pstring)

      # This is the new proof state graph representation:
      g_new, _ = gh.Graph.build_problem(p_new, DEFINITIONS)

      try:
        if run_ddar(g_new, p_new, out_file):
          logging.info('Worker PID={pid}: Solved.')
          return (i_nd, True, None)
      except Exception as e:
          logging.info(f'Worker PID={pid}: Error in run_ddar: {e}')

      # Add the candidate to the beam queue.
      ret.append( [
          # The string for the new node is old_string + lm output +
          # the special token asking for a new auxiliary point ' x00':
          # node
          (g_new, string + ' ' + lm_out + ' x00', candidate_pstring),
          # the score of each node is sum of score of all nodes
          # on the path to itself. For beam search, there is no need to
          # normalize according to path length because all nodes in beam
          # is of the same path length.
          # val
          prev_score + score ]
      )

    logging.info(f'Worker PID={pid} beam search node {i_nd}: returning')
    return (i_nd, False, ret)

def bqsearch_dask_workers(param):
  return bqsearch(param['i_nd'],param['srch_inputs'],param['out_file'])

def run_alphageometry(
    #XX model: lm.LanguageModelInference,
    p: pr.Problem,
    search_depth: int,
    beam_size: int,
    out_file: str,
) -> bool:
  """Simplified code to run AlphaGeometry proof search.

  We removed all optimizations that are infrastructure-dependent, e.g.
  parallelized model inference on multi GPUs,
  parallelized DD+AR on multiple CPUs,
  parallel execution of LM and DD+AR,
  shared pool of CPU workers across different problems, etc.

  Many other speed optimizations and abstractions are also removed to
  better present the core structure of the proof search.

  Args:
    model: Interface with inference-related endpoints to JAX's model.
    p: pr.Problem object describing the problem to solve.
    search_depth: max proof search depth.
    beam_size: beam size of the proof search.
    out_file: path to output file if solution is found.

  Returns:
    boolean of whether this is solved.
  """
  # translate the problem to a string of grammar that the LM is trained on.
  string = p.setup_str_from_problem(DEFINITIONS)
  # special tokens prompting the LM to generate auxiliary points.
  string += ' {F1} x00'
  # the graph to represent the proof state.
  g, _ = gh.Graph.build_problem(p, DEFINITIONS)

  # First we run the symbolic engine DD+AR:
  if run_ddar(g, p, out_file):
    return True
  
  # ?? when pickling graph for some problems, the default recursion limit 1000 is not enough,
  #    got 'maximum recursion depth exceeded while pickling an object' error
  sys.setrecursionlimit(10000)

  # beam search for the proof
  # each node in the search tree is a 3-tuple:
  # (<graph representation of proof state>,
  #  <string for LM to decode from>,
  #  <original problem string>)
  beam_queue = BeamQueue(max_size=beam_size)
  # originally the beam search tree starts with a single node (a 3-tuple):
  beam_queue.add(
      node=(g, string, p.txt()), val=0.0  # value of the root node is simply 0.
  )

  pool = None
  if _N_WORKSERS.value == 1:
    bqsearch_init()
  else:
    cluster = LocalCluster(
        # silence_logs=logging.INFO,
    )
    cluster.scale(_N_WORKSERS.value)
    client = Client(cluster)
    # def get_status(dask_worker):
    #   return dask_worker.status
    # logging.info(client.run(get_status))
    logging.info('Initialization start')
    param={'_GIN_FILE':_GIN_FILE.value,'_GIN_PARAM':_GIN_PARAM.value,'_GIN_SEARCH_PATHS':_GIN_SEARCH_PATHS.value,'_CKPT_PATH':_CKPT_PATH.value,'_VOCAB_PATH':_VOCAB_PATH.value}
    client.run(bqsearch_init,param=param)
    logging.info('Initialization complete')

  for depth in range(search_depth):
    logging.info(
        'Depth %s. There are %i nodes to expand:', depth, len(beam_queue)
    )
    for _, (_, string, _) in beam_queue:
      logging.info(string)

    new_queue = BeamQueue(max_size=beam_size)  # to replace beam_queue.
    if _N_WORKSERS.value==1:
      for i, srch_inputs in enumerate(beam_queue):
        _, solved, res = bqsearch(i, srch_inputs, out_file)
        if solved:
          return True
        for node, val in res:
          # Add the candidate to the beam queue.
          new_queue.add(node, val)
          # Note that the queue only maintain at most beam_size nodes
          # so this new node might possibly be dropped depending on its value.
    else:      
      work=[dict(i_nd= i, srch_inputs=srch_inputs, out_file= out_file) for i, srch_inputs in enumerate(beam_queue)]
      logging.info(work)
      if len(work)>0:
        futures=client.map(bqsearch_dask_workers,work)
        progress(futures)
        wait(futures)

        for res in futures:
          if res.result()[1]:
            client.close()
            return True
          for node,val in res.result()[2]:
            new_queue.add(node,val)

      # jobs = [pool.apply_async(bqsearch, (i, srch_inputs, out_file)) for i, srch_inputs in enumerate(beam_queue)]

      # n_done = 0
      # while n_done < len(beam_queue):
      #   for i, jobres in enumerate(jobs):
      #     if jobres and jobres.ready():            
      #       n_done += 1
      #       jobs[i] = None
      #       _, solved, res = jobres.get()
      #       if solved:
      #         # Clean up resources
      #         pool.terminate()
      #         pool.join()
      #         return True
      #       for node, val in res:
      #         # Add the candidate to the beam queue.
      #         new_queue.add(node, val)
      #         # Note that the queue only maintain at most beam_size nodes
      #         # so this new node might possibly be dropped depending on its value.            
      #   time.sleep(1)  # Adjust wait time as needed

    # replace the old queue with new queue before the new proof search depth.
    beam_queue = new_queue

  # Clean up resources
  # if pool:
  #   pool.terminate()
  #   pool.join()
  return False

def main(_):
  global DEFINITIONS
  global RULES

  # definitions of terms used in our domain-specific language.
  DEFINITIONS = pr.Definition.from_txt_file(_DEFS_FILE.value, to_dict=True)
  # load inference rules used in DD.
  RULES = pr.Theorem.from_txt_file(_RULES_FILE.value, to_dict=True)

  # when using the language model,
  # point names will be renamed to alphabetical a, b, c, d, e, ...
  # instead of staying with their original names,
  # in order to match the synthetic training data generation.
  need_rename = _MODE.value != 'ddar'

  # load problems from the problems_file,
  problems = pr.Problem.from_txt_file(
      _PROBLEMS_FILE.value, to_dict=True, translate=need_rename
  )

  if _PROBLEM_NAME.value not in problems:
    raise ValueError(
        f'Problem name `{_PROBLEM_NAME.value}` '
        + f'not found in `{_PROBLEMS_FILE.value}`'
    )

  this_problem = problems[_PROBLEM_NAME.value]

  if _MODE.value == 'ddar':
    g, _ = gh.Graph.build_problem(this_problem, DEFINITIONS)
    run_ddar(g, this_problem, _OUT_FILE.value)

  elif _MODE.value == 'alphageometry':
    #XX model = get_lm(_CKPT_PATH.value, _VOCAB_PATH.value)
    run_alphageometry(
        #XX model,
        this_problem,
        _SEARCH_DEPTH.value,
        _BEAM_SIZE.value,
        _OUT_FILE.value,
    )

  else:
    raise ValueError(f'Unknown FLAGS.mode: {_MODE.value}')


if __name__ == '__main__':
  start_time=time.time()
  app.run(main)
  logging.info("Total time is", time.strftime("%Hh %Mm %Ss", time.gmtime(time.time() - start_time)))
