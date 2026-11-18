#!/usr/bin/env python
#
# Inkscape extension making long continuous paths from shorter pieces.
# (C) 2015 juewei@fabmail.org
#
# code snippets visited to learn the extension 'effect' interface:
# - convert2dashes.py
# - http://github.com/jnweiger/inkscape-silhouette
# - http://github.com/jnweiger/inkscape-gears-dev
# - http://sourceforge.net/projects/inkcut/
# - http://code.google.com/p/inkscape2tikz/
# - http://code.google.com/p/eggbotcode/
#
# 2015-11-15 jw, V0.1 -- initial draught
# 2015-11-16 jw, V0.2 -- fixed endpoints after chaining.
# 2015-11-16 jw, V0.3 -- all possible chains connected. Yeah
# 2015-11-16 jw, V0.4 -- gui fully functional.
# 2015-11-26 jw, V0.5 -- HACK to resolve some self-reversing path segments.
#        https://github.com/fablabnbg/inkscape-chain-paths/issues/1
# 2020-04-10 jw, V0.6 -- Close paths correctly. Self reversing path hack was too eager.
#        Workaround for cubicsuperpath.parsePath/formatPath limitation.
#        Started python3 compatibility.
# 2020-05-27 vi, V0.7 -- Upgrade to Inkscape 1.0. Write debug info to inkex debug dialog.
# 2025-11-26 jm, V0.8 -- Upgrade to Inkscape 1.4

from __future__ import print_function

__version__ = '0.8'        # Keep in sync with chain_paths.inx ca line 22
__author__ = 'Juergen Weigert <juergen@fabmail.org>'
__credits__ = ['Juergen Weigert', 'Veronika Irvine', 'Jona Mainberger']

import sys, os, shutil, time, logging, tempfile, math
import re

#debug = True
debug = False

# search path, so that inkscape libraries are found when we are standalone.
sys_platform = sys.platform.lower()
if sys_platform.startswith('win'):        # windows
  sys.path.append(r'C:\Program Files\Inkscape\share\extensions')
elif sys_platform.startswith('darwin'):   # mac
  sys.path.append('/Applications/Inkscape.app/Contents/Resources/extensions')
else:                                     # linux
  # if sys_platform.startswith('linux'):
  sys.path.append('/usr/share/inkscape/extensions')

# inkscape libraries
import inkex

inkex.localization.localize()

from optparse import SUPPRESS_HELP

def uutounit(self, nn, uu):
  # viewport_to_unit follows the viewBox (unittouu does not: in a
  # document drawn in mm it is 3.78 times off)
  if hasattr(self.svg, 'viewport_to_unit'):
    return self.svg.viewport_to_unit(str(nn)+uu)
  try:
    return self.svg.unittouu(str(nn)+uu)
  except:
    return inkex.unittouu(str(nn)+uu)


def closed_subpaths(d):
  """ For each subpath of d (as CubicSuperPath splits it), whether it ends with z. """
  closed = []
  for cmd in inkex.paths.Path(d):
    if cmd.letter in 'Mm':
      closed.append(False)
    elif cmd.letter in 'Zz' and closed:
      closed[-1] = True
  return closed


def fmt(v):
  return ('%.6f' % v).rstrip('0').rstrip('.')


def format_chains(chains):
  """ Path data for chains of [handle_in, point, handle_out] nodes, each
      with its closed flag. Straight pieces are written as lines. """
  out = []
  for chain, closed in chains:
    out.append('M %s,%s' % (fmt(chain[0][1][0]), fmt(chain[0][1][1])))
    for a, b in zip(chain, chain[1:]):
      if a[2] == a[1] and b[0] == b[1]:
        out.append('L %s,%s' % (fmt(b[1][0]), fmt(b[1][1])))
      else:
        out.append('C %s,%s %s,%s %s,%s' % tuple(fmt(v) for v in (a[2][0], a[2][1], b[0][0], b[0][1], b[1][0], b[1][1])))
    if closed:
      out.append('Z')
  return ' '.join(out)

class ChainPaths(inkex.Effect):
  """
  Inkscape Extension make long continuous paths from smaller parts
  """

  def __init__(self):
    # Call the base class constructor.
    inkex.Effect.__init__(self)

    # For handling an SVG viewbox attribute, we will need to know the
    # values of the document's <svg> width and height attributes as well
    # as establishing a transform from the viewbox to the display.
    self.chain_epsilon = 0.01
    self.snap_ends = True
    self.close_loops = True
    self.segments_done = {}
    self.min_missed_distance_sq = None
    self.chained_count = 0

    self.arg_parser.add_argument('-V', '--version', dest='version', type=inkex.Boolean, default=False, help = 'Just print version number ("' + __version__ + '") and exit.')
    self.arg_parser.add_argument('-s', '--snap', dest='snap_ends', type=inkex.Boolean, default=True, help='snap end-points together when connecting')
    self.arg_parser.add_argument('-c', '--close', dest='close_loops', type=inkex.Boolean, default=True, help='close loops (start/end of the same path)')
    self.arg_parser.add_argument('-u', '--units', dest="units", type=str, default="mm", help="measurement unit for epsilon")
    self.arg_parser.add_argument('-e', '--epsilon', type=float, dest='chain_epsilon', default=0.01, help="Max. distance to connect [mm]")

  def version(self):
    return __version__
  def author(self):
    return __author__

  def calc_unit_factor(self, units='mm'):
        """ return the scale factor for all dimension conversions.
            - The document units are always irrelevant as
              everything in inkscape is expected to be in 90dpi pixel units
        """
        dialog_units = uutounit(self, 1.0, units)
        self.unit_factor = 1.0 / dialog_units
        return self.unit_factor

  def reverse_segment(self, seg):
    r = []
    for s in reversed(seg):
      # s has 3 elements: handle1, point, handle2
      # Swap handles.
      s.reverse()
      r.append(s)
    return r

  def set_segment_done(self, id, n, msg=''):
    if not id in self.segments_done:
      self.segments_done[id] = {}
    self.segments_done[id][n] = True
    if debug: inkex.utils.debug("done "+str(id)+" "+str(n)+" "+msg)

  def is_segment_done(self, id, n):
    if not id in self.segments_done:
      return False
    if n in self.segments_done[id]:
      return True
    return False

  def link_segments(self, seg1, seg2):
    if self.snap_ends:
      seg = seg1[:-1]
      p1 = seg1[-1]
      p2 = seg2[0]
      # fuse p1 and p2 to create one new point:
      # first handle from p1, point coordinates averaged, second handle from p2
      seg.append([ [  p1[0][0]                 ,  p1[0][1]                  ],
                   [ (p1[1][0] + p2[1][0]) * .5, (p1[1][1] + p2[1][1]) * .5 ],
                   [             p2[2][0]      ,             p2[2][1]       ] ])
      seg.extend(seg2[1:])
    else:
      seg = seg1[:]
      seg.extend(seg2[:])
    self.chained_count += 1
    return seg


  def is_reversal(self, sub):
    """ A 3-node subpath that goes somewhere and retraces its way back
        (a line drawn twice). Only then may the way back be dropped: two
        different curves meeting at both ends (a lens) are not one. """
    a, b, c = sub
    return (self.near_ends(c[1], a[1]) and self.near_ends(b[2], b[0]) and
            self.near_ends(c[0], a[2]))

  def near_ends(self, end1, end2):
    """ requires self.eps_sq to be the square of the near distance """
    dx = end1[0] - end2[0]
    dy = end1[1] - end2[1]
    d_sq = dx * dx + dy * dy
    if d_sq > self.eps_sq:
      if self.min_missed_distance_sq is None:
        self.min_missed_distance_sq = d_sq
      elif self.min_missed_distance_sq > d_sq:
        self.min_missed_distance_sq = d_sq
      return False
    else:
      return True


  def effect(self):
    if self.options.version:
      print(__version__)
      sys.exit(0)

    self.calc_unit_factor(self.options.units)

    if self.options.snap_ends     is not None: self.snap_ends     = self.options.snap_ends
    if self.options.close_loops   is not None: self.close_loops   = self.options.close_loops
    if self.options.chain_epsilon is not None: self.chain_epsilon = self.options.chain_epsilon
    if self.chain_epsilon < 0.001: self.chain_epsilon = 0.001        # keep a minimum.
    self.eps_sq = self.chain_epsilon * self.unit_factor * self.chain_epsilon * self.unit_factor

    if not len(self.svg.selection):
      # nothing selected (or run from the command line, which passes no
      # selection): every path in the drawing, but not those in defs
      paths = self.svg.xpath('//svg:path[not(ancestor::svg:defs)]')
      if not paths:
        inkex.errormsg(_("Please select one or more objects."))
        return
      self.svg.selection.set(*paths)

    segments = []
    for element in self.svg.selection:
      node = element
      id = element.get_id()
      if node.tag != inkex.addNS('path', 'svg'):
        inkex.errormsg(_("Object "+ str(id) + " is not a path. Try\n  - Path->Object to Path\n  - Object->Ungroup"))
        return
      if debug: inkex.utils.debug("id=" + str(id) + ", tag=" + str(node.tag))
      path_d = inkex.paths.CubicSuperPath(inkex.paths.Path(node.get('d')))
      sub_idx = -1
      for sub in path_d:
        sub_idx += 1
        # sub = [[[200.0, 300.0], [200.0, 300.0], [175.0, 290.0]], [[175.0, 265.0], [220.37694, 256.99876], [175.0, 240.0]], [[175.0, 215.0], [200.0, 200.0], [200.0, 200.0]]]
        # this is a path of three points. All the bezier handles are included. the Structure is:
        # [[handle0_OUT, point0, handle0_1], [handle1_0, point1, handle1_2], [handle2_1, point2, handle2_OUT]]
        # the _OUT handles at the end of the path are ignored. The data structure has them identical to their points.
        #
        if debug: inkex.utils.debug("   sub=" + str(sub))
        if len(sub) < 2:
          # a lone moveto draws nothing; closing it as a loop crashed
          self.set_segment_done(id, sub_idx, "lone point")
          continue
        end1 = [sub[ 0][1][0], sub[ 0][1][1]]
        end2 = [sub[-1][1][0], sub[-1][1][1]]

        # Remove trivial self revesal when building candidate segments list.
        if ((len(sub) == 3) and self.is_reversal(sub)):
          if debug: inkex.utils.debug("dropping segment from self-reversing path, length:"+str(len(sub)))
          sub.pop()
          end2 = [sub[-1][1][0], sub[-1][1][1]]

        segments.append({'id': id, 'n': sub_idx, 'end1': end1, 'end2':end2, 'seg': sub})
      if node.get(inkex.addNS('type', 'sodipodi')):
        del node.attrib[inkex.addNS('type', 'sodipodi')]
    if debug: inkex.utils.debug("-------- seen: ")
    for s in segments:
      if debug: inkex.utils.debug(str(s['id'])+", "+str(s['n'])+", "+str(s['end1'])+", "+str(s['end2']))

    # chain the segments
    obsoleted = 0
    remaining = 0
    for element in self.svg.selection:
      node = element
      id = node.get_id()
      # path_style = simplestyle.parseStyle(node.get('style'))
      path_d = inkex.paths.CubicSuperPath(inkex.paths.Path(node.get('d')))
      # which subpaths were closed with z: they stay closed even when
      # close_loops is off. Each chain gets its own closed flag.
      was_closed = closed_subpaths(node.get('d'))
      new = []
      cur_idx = -1
      for chain in path_d:
        cur_idx += 1
        if not self.is_segment_done(id, cur_idx):
          # quadratic algorithm: we check both ends of the current segment.
          # If one of them is near another known end from the segments list, we
          # chain this segment to the current segment and remove it from the
          # list,
          # end1-end1 or end2-end2: The new segment is reversed.
          # end1-end2: The new segment is prepended to the current segment.
          # end2-end1: The new segment is appended to the current segment.
          self.set_segment_done(id, cur_idx, "output")        # do not cross with ourselves.
          end1 = [chain[ 0][1][0], chain[ 0][1][1]]
          end2 = [chain[-1][1][0], chain[-1][1][1]]

          # Remove trivial self revesal when doing the actual chain operation.
          if ((len(chain) == 3) and self.is_reversal(chain)):
            chain.pop()
            end2 = [chain[-1][1][0], chain[-1][1][1]]

          segments_idx = 0
          while segments_idx < len(segments):
            seg = segments[segments_idx]
            if self.is_segment_done(seg['id'], seg['n']):
              segments_idx += 1
              continue

            if (self.near_ends(end1, seg['end1']) or
                self.near_ends(end2, seg['end2'])):
              seg['seg'] = self.reverse_segment(seg['seg'])
              seg['end1'], seg['end2'] = seg['end2'], seg['end1']
              if debug: inkex.utils.debug("reversed seg " + str(seg['id']) + ", "+str(seg['n']))

            if self.near_ends(end1, seg['end2']):
              # prepend seg to chain
              self.set_segment_done(seg['id'], seg['n'], 'prepended to ' + str(id) + ' ' + str(cur_idx))
              chain = self.link_segments(seg['seg'], chain)
              end1 = [chain[0][1][0], chain[0][1][1]]
              segments_idx = 0          # this chain changed. re-visit all candidate
              continue

            if self.near_ends(end2, seg['end1']):
              # append seg to chain
              self.set_segment_done(seg['id'], seg['n'], 'appended to ' + str(id) + ' ' + str(cur_idx))
              chain = self.link_segments(chain, seg['seg'])
              end2 = [chain[-1][1][0], chain[-1][1][1]]
              segments_idx = 0          # this chain changed. re-visit all candidate
              continue

            segments_idx += 1

          # Now all joinable segments are joined.
          # Finally, we can check, if the resulting path is a closed path:
          # Closing a path here, isolates it from the rest.
          # But as we prefer to make the chain as long as possible, we close late.
          started_closed = cur_idx < len(was_closed) and was_closed[cur_idx]
          chain_closed = False
          if len(chain) >= 2 and self.near_ends(end1, end2) and (self.close_loops or started_closed):
              if debug: inkex.utils.debug("closing closeable loop " +str(id))
              if self.snap_ends:
                  # move both ends to their mid position, handles along;
                  # the last piece stays (dropping it, and letting z
                  # draw a straight line instead, lost curves)
                  xm = (chain[0][1][0] + chain[-1][1][0]) * 0.5
                  ym = (chain[0][1][1] + chain[-1][1][1]) * 0.5
                  for node_ in (chain[0], chain[-1]):
                      dx, dy = xm - node_[1][0], ym - node_[1][1]
                      for pt in node_:
                          pt[0] += dx
                          pt[1] += dy
              chain_closed = True
              self.chained_count +=1

          new.append((chain, chain_closed))

      if not len(new):
        #node.clear()
        node.delete()
        obsoleted += 1
        if debug: inkex.utils.debug("Path node obsoleted: " +str(id))
      else:
        remaining += 1
        p_fmt = format_chains(new)
        if debug: inkex.utils.debug("new path: "+str(p_fmt))
        node.set('d', p_fmt)

    # statistics:
    if debug: inkex.utils.debug("Path nodes obsoleted: "+str(obsoleted) + "\nPath nodes remaining:"+str(remaining))
    if self.min_missed_distance_sq is not None:
      if debug: inkex.utils.debug("min_missed_distance: "+str(math.sqrt(float(self.min_missed_distance_sq))/self.unit_factor)+'>'+str(self.chain_epsilon)+str(self.options.units))
    if debug: inkex.utils.debug("Successful link operations: "+str(self.chained_count))

if __name__ == '__main__':
        e = ChainPaths()

        e.run()
        sys.exit(0)    # helps to keep the selection
