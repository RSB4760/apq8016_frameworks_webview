#!/usr/bin/env python
#
# Copyright (C) 2012 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Merge Chromium into the Android tree. See the output of --help for details.

"""
import optparse
import os
import re
import subprocess
import sys
import urllib2


# We need to import this *after* merging from upstream to get the latest
# version. Set it to none here to catch uses before it's imported.
webview_licenses = None


REPOSITORY_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '../../../external/chromium_org'))
AUTOGEN_MESSAGE = 'This commit was generated by merge-from-chromium.py.'


# Whitelist of projects that need to be merged to build WebView. We don't need
# the other upstream repositories used to build the actual Chrome app.
THIRD_PARTY_PROJECTS = [
    'googleurl',
    'sdch/open-vcdiff',
    'testing/gmock',
    'testing/gtest',
    'third_party/WebKit',
    'third_party/angle',
    'third_party/cacheinvalidation/files/src/google',
    'third_party/freetype',
    'third_party/hunspell',
    'third_party/hunspell_dictionaries',
    'third_party/icu',
    'third_party/leveldatabase/src',
    'third_party/libjingle/source',
    'third_party/libphonenumber/src/phonenumbers',
    'third_party/libphonenumber/src/resources',
    'third_party/libphonenumber/src/test',
    'third_party/openssl',
    'third_party/ots',
    'third_party/pyftpdlib/src',
    'third_party/skia/include',
    'third_party/skia/gyp',
    'third_party/skia/src',
    'third_party/smhasher/src',
    'tools/grit',
    'tools/gyp',
    'v8',
]


ALL_PROJECTS = ['.'] + THIRD_PARTY_PROJECTS


def _GetCommandStdout(args, cwd=REPOSITORY_ROOT, ignore_errors=False):
  """Gets stdout from runnng the specified shell command.
  Args:
    args: The command and its arguments.
    cwd: The working directory to use. Defaults to REPOSITORY_ROOT.
    ignore_errors: Ignore the command's return code.
  Returns:
    stdout from running the command.
  """

  p = subprocess.Popen(args=args, cwd=cwd, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)
  stdout, stderr = p.communicate()
  if p.returncode == 0 or ignore_errors:
    return stdout
  else:
    print >>sys.stderr, 'Running command %s failed:' % args
    print >>sys.stderr, stderr
    raise Exception('Command execution failed')


def _ReadGitFile(git_url, git_branch, sha1, path):
  """Reads a file from a remote git project at a specific revision.
  Args:
    git_url: The URL of the git server.
    git_branch: The branch to read.
    sha1: The SHA1 at which to read.
    path: The relative path of the file to read.
  Returns:
    The contents of the specified file.
  """

  # We fetch the branch to a temporary head so that we don't download the same
  # commits multiple times.
  _GetCommandStdout(['git', 'fetch', '-f', git_url,
                     git_branch + ':cached_upstream'])

  args = ['git', 'show', '%s:%s' % (sha1, path)]
  return _GetCommandStdout(args)


def _ParseDEPS(git_url, git_branch, sha1):
  """Parses the .DEPS.git file from Chromium and returns its contents.
  Args:
    git_url: The URL of the git server.
    git_branch: The branch to read.
    sha1: The SHA1 at which to read.
  Returns:
    A dictionary of the contents of .DEPS.git at the specified revision
  """

  class FromImpl(object):
    """Used to implement the From syntax."""

    def __init__(self, module_name):
      self.module_name = module_name

    def __str__(self):
      return 'From("%s")' % self.module_name

  class _VarImpl(object):
    def __init__(self, custom_vars, local_scope):
      self._custom_vars = custom_vars
      self._local_scope = local_scope

    def Lookup(self, var_name):
      """Implements the Var syntax."""
      if var_name in self._custom_vars:
        return self._custom_vars[var_name]
      elif var_name in self._local_scope.get('vars', {}):
        return self._local_scope['vars'][var_name]
      raise Exception('Var is not defined: %s' % var_name)

  tmp_locals = {}
  var = _VarImpl({}, tmp_locals)
  tmp_globals = {'From': FromImpl, 'Var': var.Lookup, 'deps_os': {}}
  deps_content = _ReadGitFile(git_url, git_branch, sha1, '.DEPS.git')
  exec(deps_content) in tmp_globals, tmp_locals
  return tmp_locals


def _GetThirdPartyProjectMergeInfo(third_party_projects, deps_vars):
  """Gets the git URL for each project and the SHA1 at which it should be
  merged.
  Args:
    third_party_projects: The list of projects to consider.
    deps_vars: The dictionary of dependencies from .DEPS.git.
  Returns:
    A dictionary from project to git URL and SHA1 - 'path: (url, sha1)'
  """

  deps_fallback_order = [
      deps_vars['deps'],
      deps_vars['deps_os']['unix'],
      deps_vars['deps_os']['android'],
  ]
  result = {}
  for path in third_party_projects:
    for deps in deps_fallback_order:
      url_plus_sha1 = deps.get(os.path.join('src', path))
      if url_plus_sha1:
        break
    else:
      raise RuntimeError(
          ('Could not find .DEPS.git entry for project %s. This probably '
           'means that the project list in snapshot.py needs to be updated.') %
          path)
    match = re.match('(.*?)@(.*)', url_plus_sha1)
    url = match.group(1)
    sha1 = match.group(2)
    print '  Got URL %s and SHA1 %s for project %s' % (url, sha1, path)
    result[path] = {'url': url, 'sha1': sha1}
  return result


def _CheckNoConflictsAndCommitMerge(commit_message, cwd=REPOSITORY_ROOT):
  """Prompts the user to resolve merge conflicts then once done, commits the
  merge using either the supplied commit message or a user-supplied override.
  Args:
    commit_message: The default commit message
  """

  status = _GetCommandStdout(['git', 'status', '--porcelain'], cwd=cwd)
  conflicts_deleted_by_us = [x[1] for x in re.findall(r'^(DD|DU) ([^\n]+)$',
                                                      status,
                                                      flags=re.MULTILINE)]
  if conflicts_deleted_by_us:
    print 'Keeping ours for the following locally deleted files.\n  %s' % \
        '\n  '.join(conflicts_deleted_by_us)
    _GetCommandStdout(['git', 'rm', '-rf', '--ignore-unmatch'] +
                      conflicts_deleted_by_us, cwd=cwd)

  while True:
    status = _GetCommandStdout(['git', 'status', '--porcelain'], cwd=cwd)
    conflicts = re.findall(r'^((DD|AU|UD|UA|DU|AA|UU) [^\n]+)$', status,
                           flags=re.MULTILINE)
    if not conflicts:
      break
    conflicts_string = '\n'.join([x[0] for x in conflicts])
    new_commit_message = raw_input(
        ('The following conflicts exist and must be resolved.\n\n%s\n\nWhen '
         'done, enter a commit message or press enter to use the default '
         '(\'%s\').\n\n') % (conflicts_string, commit_message))
    if new_commit_message:
      commit_message = new_commit_message
  _GetCommandStdout(['git', 'commit', '-m', commit_message], cwd=cwd)


def _MergeProjects(git_url, git_branch, svn_revision, root_sha1):
  """Merges into this repository all projects required by the specified branch
  of Chromium, at the SVN revision. Uses a git subtree merge for each project.
  Directories in the main Chromium repository which are not needed by Clank are
  not merged.
  Args:
    git_url: The URL of the git server for the Chromium branch to merge to
    git_branch: The branch name to merge to
    svn_revision: The SVN revision for the main Chromium repository
    root_sha1: The git SHA1 for the main Chromium repository
  """

  # The logic for this step lives here, in the Android tree, as it makes no
  # sense for a Chromium tree to know about this merge.

  print 'Parsing DEPS ...'
  deps_vars = _ParseDEPS(git_url, git_branch, root_sha1)

  merge_info = _GetThirdPartyProjectMergeInfo(THIRD_PARTY_PROJECTS, deps_vars)

  for path in merge_info:
    url = merge_info[path]['url']
    sha1 = merge_info[path]['sha1']
    dest_dir = os.path.join(REPOSITORY_ROOT, path)
    _GetCommandStdout(['git', 'checkout', '-b', 'merge-from-chromium',
                       '-t', 'goog/master-chromium'], cwd=dest_dir)
    print 'Fetching project %s at %s ...' % (path, sha1)
    _GetCommandStdout(['git', 'fetch', url], cwd=dest_dir)
    if _GetCommandStdout(['git', 'rev-list', '-1', 'HEAD..' + sha1],
                         cwd=dest_dir):
      print 'Merging project %s at %s ...' % (path, sha1)
      # Merge conflicts make git merge return 1, so ignore errors
      _GetCommandStdout(['git', 'merge', '--no-commit', sha1], cwd=dest_dir,
                        ignore_errors=True)
      _CheckNoConflictsAndCommitMerge(
          'Merge %s from %s at %s\n\n%s' % (path, url, sha1, AUTOGEN_MESSAGE),
          cwd=dest_dir)
    else:
      print 'No new commits to merge in project %s' % path

  # Handle root repository separately.
  _GetCommandStdout(['git', 'checkout', '-b', 'merge-from-chromium',
                     '-t', 'goog/master-chromium'])
  print 'Fetching Chromium at %s ...' % root_sha1
  _GetCommandStdout(['git', 'fetch', git_url, git_branch])
  print 'Merging Chromium at %s ...' % root_sha1
  # Merge conflicts make git merge return 1, so ignore errors
  _GetCommandStdout(['git', 'merge', '--no-commit', root_sha1],
                    ignore_errors=True)
  _CheckNoConflictsAndCommitMerge(
      'Merge Chromium from %s branch %s at r%s (%s)\n\n%s'
      % (git_url, git_branch, svn_revision, root_sha1, AUTOGEN_MESSAGE))

  print 'Getting directories to exclude ...'

  # We import this now that we have merged the latest version.
  # It imports to a global in order that it can be used to generate NOTICE
  # later. We also disable writing bytecode to keep the source tree clean.
  sys.path.append(os.path.join(REPOSITORY_ROOT, 'android_webview', 'tools'))
  sys.dont_write_bytecode = True
  global webview_licenses
  import webview_licenses
  import known_incompatible

  for path, exclude_list in known_incompatible.KNOWN_INCOMPATIBLE.iteritems():
    print '  %s' % '\n  '.join(os.path.join(path, x) for x in exclude_list)
    dest_dir = os.path.join(REPOSITORY_ROOT, path)
    _GetCommandStdout(['git', 'rm', '-rf', '--ignore-unmatch'] + exclude_list,
                      cwd=dest_dir)
    if _ModifiedFilesInIndex(dest_dir):
      _GetCommandStdout(['git', 'commit', '-m',
                         'Exclude incompatible directories'], cwd=dest_dir)

  directories_left_over = webview_licenses.GetIncompatibleDirectories()
  if directories_left_over:
    raise RuntimeError('Incompatibly licensed directories remain: ' +
                       '\n'.join(directories_left_over))
  return True


def _GenerateMakefiles(svn_revision):
  """Run gyp to generate the makefiles required to build Chromium in the
  Android build system.
  """

  print 'Regenerating makefiles ...'
  # TODO(torne): The .tmp files are generated by
  # third_party/WebKit/Source/WebCore/WebCore.gyp/WebCore.gyp into the source
  # tree. We should avoid this, or at least use a more specific name to avoid
  # accidentally removing or adding other files.
  for path in ALL_PROJECTS:
    dest_dir = os.path.join(REPOSITORY_ROOT, path)
    _GetCommandStdout(['git', 'rm', '--ignore-unmatch', 'GypAndroid.mk',
                       '*.target.mk', '*.host.mk', '*.tmp'], cwd=dest_dir)

  _GetCommandStdout(['bash', '-c', 'export CHROME_ANDROID_BUILD_WEBVIEW=1 && '
                                   'export CHROME_SRC=`pwd` && '
                                   'export PYTHONDONTWRITEBYTECODE=1 && '
                                   '. build/android/envsetup.sh && '
                                   'android_gyp'])

  for path in ALL_PROJECTS:
    dest_dir = os.path.join(REPOSITORY_ROOT, path)
    # git add doesn't have an --ignore-unmatch so we have to do this instead:
    _GetCommandStdout(['git', 'add', '-f', 'GypAndroid.mk'], ignore_errors=True,
                      cwd=dest_dir)
    _GetCommandStdout(['git', 'add', '-f', '*.target.mk'], ignore_errors=True,
                      cwd=dest_dir)
    _GetCommandStdout(['git', 'add', '-f', '*.host.mk'], ignore_errors=True,
                      cwd=dest_dir)
    _GetCommandStdout(['git', 'add', '-f', '*.tmp'], ignore_errors=True,
                      cwd=dest_dir)
    # Only try to commit the makefiles if something has actually changed.
    if _ModifiedFilesInIndex(dest_dir):
      _GetCommandStdout(['git', 'commit', '-m',
                         'Update makefiles after merge of Chromium at r%s\n\n%s'
                         % (svn_revision, AUTOGEN_MESSAGE)], cwd=dest_dir)


def _ModifiedFilesInIndex(cwd=REPOSITORY_ROOT):
  """Returns whether git's index includes modified files, ie 'added' changes.
  """
  status = _GetCommandStdout(['git', 'status', '--porcelain'], cwd=cwd)
  return re.search(r'^[MADRC]', status, flags=re.MULTILINE) != None


def _GenerateNoticeFile(svn_revision):
  """Generates a NOTICE file for all third-party code (from Android's
  perspective) that lives in the Chromium tree and commits it to the root of
  the repository.
  Args:
    svn_revision: The SVN revision for the main Chromium repository
  """

  print 'Regenerating NOTICE file ...'

  contents = webview_licenses.GenerateNoticeFile()

  with open(os.path.join(REPOSITORY_ROOT, 'NOTICE'), 'w') as f:
    f.write(contents)
  _GetCommandStdout(['git', 'add', 'NOTICE'])
  # Only try to commit the NOTICE update if the file has actually changed.
  if _ModifiedFilesInIndex():
    _GetCommandStdout([
        'git', 'commit', '-m',
        'Update NOTICE file after merge of Chromium at r%s\n\n%s'
        % (svn_revision, AUTOGEN_MESSAGE)])


def _GetSVNRevisionAndSHA1(git_url, git_branch, svn_revision):
  print 'Getting SVN revision and SHA1 ...'
  _GetCommandStdout(['git', 'fetch', '-f', git_url,
                     git_branch + ':cached_upstream'])
  if svn_revision:
    # Sometimes, we see multiple commits with the same git SVN ID. No idea why.
    # We take the most recent.
    sha1 = _GetCommandStdout(['git', 'log',
                              '--grep=git-svn-id: .*@%s' % svn_revision,
                              '--format=%H', 'cached_upstream']).split()[0]
  else:
    # Just use the latest commit.
    # TODO: We may be able to use a LKGR?
    commit = _GetCommandStdout(['git', 'log', '-n1', '--grep=git-svn-id:',
                                '--format=%H%n%b', 'cached_upstream'])
    sha1 = commit.split()[0]
    svn_revision = re.search(r'^git-svn-id: .*@([0-9]+)', commit,
                             flags=re.MULTILINE).group(1)
  return (svn_revision, sha1)


def _Snapshot(git_url, git_branch, svn_revision):
  """Takes a snapshot of the specified Chromium tree at the specified SVN
  revision and merges it into this repository. Also generates Android makefiles
  and generates a top-level NOTICE file suitable for use in the Android build.
  Args:
    git_url: The URL of the git server for the Chromium branch to merge to
    svn_revision: The SVN revision for the main Chromium repository
  """

  (svn_revision, root_sha1) = _GetSVNRevisionAndSHA1(git_url, git_branch,
                                                     svn_revision)
  if not _GetCommandStdout(['git', 'rev-list', '-1', 'HEAD..' + root_sha1]):
    print ('No new commits to merge from %s branch %s at r%s (%s)' %
        (git_url, git_branch, svn_revision, root_sha1))
    return

  print ('Snapshotting Chromium from %s branch %s at r%s (%s)' %
         (git_url, git_branch, svn_revision, root_sha1))

  # 1. Merge, accounting for excluded directories
  _MergeProjects(git_url, git_branch, svn_revision, root_sha1)

  # 2. Generate Android NOTICE file
  _GenerateNoticeFile(svn_revision)

  # 3. Generate Android makefiles
  _GenerateMakefiles(svn_revision)


def main():
  parser = optparse.OptionParser(usage='%prog [options]')
  parser.epilog = ('Takes a snapshot of the Chromium tree at the specified '
                   'Chromium SVN revision and merges it into this repository. '
                   'Paths marked as excluded for license reasons are removed '
                   'as part of the merge. Also generates Android makefiles and '
                   'generates a top-level NOTICE file suitable for use in the '
                   'Android build.')
  parser.add_option(
      '', '--git_url',
      default='http://git.chromium.org/chromium/src.git',
      help=('The URL of the git server for the Chromium branch to merge. '
            'Defaults to upstream.'))
  parser.add_option(
      '', '--git_branch',
      default='git-svn',
      help=('The name of the upstream branch to merge. Defaults to git-svn.'))
  parser.add_option(
      '', '--svn_revision',
      default=None,
      help=('Merge to the specified chromium SVN revision, rather than using '
            'the current latest revision.'))
  (options, args) = parser.parse_args()
  if args:
    parser.print_help()
    return 1

  if 'ANDROID_BUILD_TOP' not in os.environ:
    print >>sys.stderr, 'You need to run the Android envsetup.sh and lunch.'
    return 1

  if not _Snapshot(options.git_url, options.git_branch, options.svn_revision):
    return 1

  return 0

if __name__ == '__main__':
  sys.exit(main())
