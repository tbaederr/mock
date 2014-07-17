import fcntl
import glob
import grp
import logging
import os
import pwd
import shutil
import stat

from mockbuild import util
from mockbuild import mounts
from mockbuild.exception import BuildRootLocked, RootError, \
                                ResultDirNotAccessible, Error
from mockbuild.package_manager import PackageManager
from mockbuild.trace_decorator import getLog

class Buildroot(object):
    def __init__(self, config, uid_manager, state, plugins):
        self.config = config
        self.uid_manager = uid_manager
        self.state = state
        self.plugins = plugins
        if config.has_key('unique-ext'):
            config['root'] = "%s-%s" % (config['root'], config['unique-ext'])
        self.basedir = os.path.join(config['basedir'], config['root'])
        self.rootdir = os.path.join(self.basedir, 'root')
        self.resultdir = config['resultdir'] % config
        self.homedir = config['chroothome']
        self.shared_root_name = config['root']
        self.cache_topdir = config['cache_topdir']
        self.cachedir = os.path.join(self.cache_topdir, self.shared_root_name)
        self.builddir = os.path.join(self.homedir, 'build')
        self._lock_file = None
        self.selinux = (not self.config['plugin_conf']['selinux_enable']
                        and util.selinuxEnabled())

        self.chrootuid = config['chrootuid']
        self.chrootuser = 'mockbuild'
        self.chrootgid = config['chrootgid']
        self.chrootgroup = 'mockbuild'
        self.env = config['environment']
        proxy_env = util.get_proxy_environment(config)
        self.env.update(proxy_env)
        os.environ.update(proxy_env)

        self.pkg_manager = PackageManager(config, self, plugins)
        self.mounts = mounts.Mounts(self)

        self.root_log = getLog("mockbuild")
        self.build_log = getLog("mockbuild.Root.build")
        self.logging_initialized = False
        self.chroot_was_initialized = False
        self.preexisting_deps = []
        self.plugins.init_plugins(self)

    def make_chroot_path(self, *paths):
        new_path = self.rootdir
        for path in paths:
            if path.startswith('/'):
                path = path[1:]
            new_path = os.path.join(new_path, path)
        return new_path

    def initialize(self):
        """
        Initialize the builroot to a point where it's possible to execute
        commands in chroot. If it was already initialized, just lock the shared
        lock.
        """
        try:
            self._lock_buildroot(exclusive=True)
            self._init()
        except BuildRootLocked:
            pass
        finally:
            self._lock_buildroot(exclusive=False)
        self._resetLogging()

    def chroot_is_initialized(self):
        return os.path.exists(self.make_chroot_path('.initialized'))

    def _init(self):
        # If previous run didn't finish properly
        self._umount_residual()

        self.state.start("chroot init")
        self.chroot_was_initialized = self.chroot_is_initialized()
        getLog().info("calling preinit hooks")
        self.plugins.call_hooks('preinit')
        self.chroot_was_initialized = self.chroot_is_initialized()

        if not self.chroot_was_initialized:
            self._setup_dirs()
            self._setup_devices()
            self._setup_files()
            self.mounts.mountall()
            self._resetLogging()

            # write out config details
            self.root_log.debug('rootdir = %s' % self.make_chroot_path())
            self.root_log.debug('resultdir = %s' % self.resultdir)

            self._setup_resolver_config()
            self._setup_dbus_uuid()
            self._init_aux_files()
            self._setup_timezone()
            self._init_pkg_management()
            self._make_build_user()
            self._setup_build_dirs()
        else:
            self._setup_devices()
            self.mounts.mountall()

        # mark the buildroot as initialized
        util.touch(self.make_chroot_path('.initialized'))

        # done with init
        self.plugins.call_hooks('postinit')
        self.state.finish("chroot init")

    # bad hack
    # comment out decorator here so we dont get double exceptions in the root log
    def doChroot(self, command, shell=True, *args, **kargs):
        """execute given command in root"""
        if not util.hostIsEL5():
            self._nuke_rpm_db()
        return util.do(command, chrootPath=self.make_chroot_path(),
                                 env=self.env, shell=shell, *args, **kargs)

    def _setup_resolver_config(self):
        if self.config['use_host_resolv']:
            etcdir = self.make_chroot_path('etc')

            resolvconfpath = self.make_chroot_path('etc', 'resolv.conf')
            if os.path.exists(resolvconfpath):
                os.remove(resolvconfpath)
            shutil.copy2('/etc/resolv.conf', etcdir)

            hostspath = self.make_chroot_path('etc', 'hosts')
            if os.path.exists(hostspath):
                os.remove(hostspath)
            shutil.copy2('/etc/hosts', etcdir)

    def _setup_dbus_uuid(self):
        try:
            import uuid
            machine_uuid = uuid.uuid4().hex
            dbus_uuid_path = self.make_chroot_path('var', 'lib', 'dbus', 'machine-id')
            with open(dbus_uuid_path, 'w') as uuid_file:
                uuid_file.write(machine_uuid)
                uuid_file.write('\n')
        except ImportError:
            pass

    def _setup_timezone(self):
        localtimedir = self.make_chroot_path('etc')
        localtimepath = self.make_chroot_path('etc', 'localtime')
        if os.path.exists(localtimepath):
            os.remove(localtimepath)
        shutil.copy2('/etc/localtime', localtimedir)

    def _init_pkg_management(self):
        self.pkg_manager.initialize_config()
        update_state = '{0} update'.format(self.pkg_manager.command)
        self.state.start(update_state)
        if not self.chroot_was_initialized:
            cmd = self.config['chroot_setup_cmd']
            if isinstance(cmd, basestring):
                cmd = cmd.split()
            self.pkg_manager.execute(*cmd)
        self.state.finish(update_state)

    def _make_build_user(self):
        if not os.path.exists(self.make_chroot_path('usr/sbin/useradd')):
            raise RootError("Could not find useradd in chroot, maybe the install failed?")

        if self.config['clean']:
            # safe and easy. blow away existing /builddir and completely re-create.
            util.rmtree(self.make_chroot_path(self.homedir), selinux=self.selinux)

        dets = {'uid': str(self.chrootuid), 'gid': str(self.chrootgid), 'user': self.chrootuser, 'group': self.chrootgroup, 'home': self.homedir}

        # ok for these two to fail
        self.doChroot(['/usr/sbin/userdel', '-r', '-f', dets['user']], shell=False, raiseExc=False)
        self.doChroot(['/usr/sbin/groupdel', dets['group']], shell=False, raiseExc=False)

        self.doChroot(['/usr/sbin/groupadd', '-g', dets['gid'], dets['group']], shell=False)
        self.doChroot(self.config['useradd'] % dets, shell=True)
        self._enable_chrootuser_account()

    def _enable_chrootuser_account(self):
        passwd = self.make_chroot_path('/etc/passwd')
        lines = open(passwd).readlines()
        disabled = False
        newlines = []
        for l in lines:
            parts = l.strip().split(':')
            if parts[0] == self.chrootuser and parts[1].startswith('!!'):
                disabled = True
                parts[1] = parts[1][2:]
            newlines.append(':'.join(parts))
        if disabled:
            f = open(passwd, "w")
            for l in newlines:
                f.write(l+'\n')
            f.close()

    def _resetLogging(self):
        # ensure we dont attach the handlers multiple times.
        if self.logging_initialized:
            return
        self.logging_initialized = True

        util.mkdirIfAbsent(self.resultdir)

        try:
            self.uid_manager.dropPrivsTemp()

            # attach logs to log files.
            # This happens in addition to anything that
            # is set up in the config file... ie. logs go everywhere
            for (log, filename, fmt_str) in (
                    (self.state.state_log, "state.log", self.config['state_log_fmt_str']),
                    (self.build_log, "build.log", self.config['build_log_fmt_str']),
                    (self.root_log, "root.log", self.config['root_log_fmt_str'])):
                fullPath = os.path.join(self.resultdir, filename)
                fh = logging.FileHandler(fullPath, "a+")
                formatter = logging.Formatter(fmt_str)
                fh.setFormatter(formatter)
                fh.setLevel(logging.NOTSET)
                log.addHandler(fh)
                log.info("Mock Version: %s" % self.config['version'])
        finally:
            self.uid_manager.restorePrivs()

    def _init_aux_files(self):
        chroot_file_contents = self.config['files']
        for key in chroot_file_contents:
            p = self.make_chroot_path(key)
            if not os.path.exists(p):
                util.mkdirIfAbsent(os.path.dirname(p))
                with open(p, 'w+') as fo:
                    fo.write(chroot_file_contents[key])

    def _nuke_rpm_db(self):
        """remove rpm DB lock files from the chroot"""

        dbfiles = glob.glob(self.make_chroot_path('var/lib/rpm/__db*'))
        if not dbfiles:
            return
        self.root_log.debug("removing %d rpm db files" % len(dbfiles))
        # become root
        self.uid_manager.becomeUser(0, 0)
        try:
            for tmp in dbfiles:
                self.root_log.debug("_nuke_rpm_db: removing %s" % tmp)
                try:
                    os.unlink(tmp)
                except OSError as e:
                    getLog().error("%s" % e)
                    raise
        finally:
            self.uid_manager.restorePrivs()

    def _open_lock(self):
        util.mkdirIfAbsent(self.basedir)
        self._lock_file = open(os.path.join(self.basedir, "buildroot.lock"), "a+")

    def _lock_buildroot(self, exclusive):
        lock_type = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if not self._lock_file:
            self._open_lock()
        try:
            fcntl.lockf(self._lock_file.fileno(), lock_type | fcntl.LOCK_NB)
        except IOError:
            raise BuildRootLocked("Build root is locked by another process.")

    def _unlock_buildroot(self):
        if self._lock_file:
            self._lock_file.close()
        self._lock_file = None

    def _setup_dirs(self):
        self.root_log.debug('create skeleton dirs')
        dirs = ['var/lib/rpm',
                     'var/lib/yum',
                     'var/lib/dbus',
                     'var/log',
                     'var/cache/yum',
                     'etc/rpm',
                     'tmp',
                     'tmp/ccache',
                     'var/tmp',
                     #dnf?
                     'etc/yum.repos.d',
                     'etc/yum',
                     'proc',
                     'sys']
        dirs += self.config['extra_chroot_dirs']
        for item in dirs:
            util.mkdirIfAbsent(self.make_chroot_path(item))
        self.uid_manager.dropPrivsTemp()
        try:
            util.mkdirIfAbsent(self.resultdir)
        except Error:
            raise ResultDirNotAccessible(ResultDirNotAccessible.__doc__ % self.resultdir)
        finally:
            self.uid_manager.restorePrivs()

    def _setup_build_dirs(self):
        build_dirs = ['RPMS', 'SPECS', 'SRPMS', 'SOURCES', 'BUILD', 'BUILDROOT',
                      'originals']
        self.uid_manager.dropPrivsTemp()
        try:
            for item in build_dirs:
                util.mkdirIfAbsent(self.make_chroot_path(self.builddir, item))

            # change ownership so we can write to build home dir
            for (dirpath, dirnames, filenames) in os.walk(self.make_chroot_path(self.homedir)):
                for path in dirnames + filenames:
                    os.chown(os.path.join(dirpath, path), self.chrootuid, -1)
                    os.chmod(os.path.join(dirpath, path), 0755)

            # rpmmacros default
            macrofile_out = self.make_chroot_path(self.homedir, ".rpmmacros")
            rpmmacros = open(macrofile_out, 'w+')
            for key, value in self.config['macros'].items():
                rpmmacros.write("%s %s\n" % (key, value))
            rpmmacros.close()
        finally:
            self.uid_manager.restorePrivs()

    def _setup_devices(self):
        if self.config['internal_dev_setup']:
            util.rmtree(self.make_chroot_path("dev"), selinux=self.selinux)
            util.mkdirIfAbsent(self.make_chroot_path("dev", "pts"))
            util.mkdirIfAbsent(self.make_chroot_path("dev", "shm"))
            prevMask = os.umask(0000)
            devFiles = [
                (stat.S_IFCHR | 0666, os.makedev(1, 3), "dev/null"),
                (stat.S_IFCHR | 0666, os.makedev(1, 7), "dev/full"),
                (stat.S_IFCHR | 0666, os.makedev(1, 5), "dev/zero"),
                (stat.S_IFCHR | 0666, os.makedev(1, 8), "dev/random"),
                (stat.S_IFCHR | 0444, os.makedev(1, 9), "dev/urandom"),
                (stat.S_IFCHR | 0666, os.makedev(5, 0), "dev/tty"),
                (stat.S_IFCHR | 0600, os.makedev(5, 1), "dev/console"),
                (stat.S_IFCHR | 0666, os.makedev(5, 2), "dev/ptmx"),
                ]
            kver = os.uname()[2]
            #getLog().debug("kernel version == %s" % kver)
            for i in devFiles:
                # create node
                os.mknod(self.make_chroot_path(i[2]), i[0], i[1])
                # set context. (only necessary if host running selinux enabled.)
                # fails gracefully if chcon not installed.
                if self.selinux:
                    util.do(
                        ["chcon", "--reference=/" + i[2], self.make_chroot_path(i[2])],
                         raiseExc=0, shell=False, env=self.env)

            os.symlink("/proc/self/fd/0", self.make_chroot_path("dev/stdin"))
            os.symlink("/proc/self/fd/1", self.make_chroot_path("dev/stdout"))
            os.symlink("/proc/self/fd/2", self.make_chroot_path("dev/stderr"))

            if os.path.isfile(self.make_chroot_path('etc', 'mtab')):
                os.remove(self.make_chroot_path('etc', 'mtab'))
                os.symlink("/proc/self/mounts", self.make_chroot_path('etc', 'mtab'))

            os.chown(self.make_chroot_path('dev/tty'), pwd.getpwnam('root')[2], grp.getgrnam('tty')[2])
            os.chown(self.make_chroot_path('dev/ptmx'), pwd.getpwnam('root')[2], grp.getgrnam('tty')[2])

            # symlink /dev/fd in the chroot for everything except RHEL4
            if util.cmpKernelVer(kver, '2.6.9') > 0:
                os.symlink("/proc/self/fd", self.make_chroot_path("dev/fd"))

            os.umask(prevMask)

            if util.cmpKernelVer(kver, '2.6.18') >= 0:
                os.unlink(self.make_chroot_path('/dev/ptmx'))
            os.symlink("pts/ptmx", self.make_chroot_path('/dev/ptmx'))

    def _setup_files(self):
        #self.root_log.debug('touch required files')
        for item in [self.make_chroot_path('etc', 'fstab'),
                     self.make_chroot_path('var', 'log', 'yum.log')]:
            util.touch(item)

    def finalize(self):
        """
        Do the cleanup if this is the last process working with the buildroot.
        """
        if os.path.exists(self.make_chroot_path()):
            try:
                self._lock_buildroot(exclusive=True)
                util.orphansKill(self.make_chroot_path())
                self._umount_all()
            except BuildRootLocked:
                pass
            finally:
                self._unlock_buildroot()

    def delete(self):
        """
        Deletes the buildroot contents.
        """
        if os.path.exists(self.basedir):
            self._lock_buildroot(exclusive=True)
            util.orphansKill(self.make_chroot_path())
            self._umount_all()
            self._unlock_buildroot()
            util.rmtree(self.basedir, selinux=self.selinux)
        self.chroot_was_initialized = False

    def _umount_all(self):
        """umount all mounted chroot fs."""

        # first try removing all expected mountpoints.
        self.mounts.umountall()

        # then remove anything that might be left around.
        self._umount_residual()

    def _mount_is_ours(self, mountpoint):
        mountpoint = os.path.realpath(mountpoint)
        our_dir = os.path.realpath(self.make_chroot_path()) + '/'
        assert our_dir and our_dir != '/'
        if mountpoint.startswith(our_dir):
            return True
        return False


    def _umount_residual(self):
        mountpoints = open("/proc/mounts").read().strip().split("\n")

        # umount in reverse mount order to prevent nested mount issues that
        # may prevent clean unmount.
        for mountline in reversed(mountpoints):
            mountpoint = mountline.split()[1]
            if self._mount_is_ours(mountpoint):
                cmd = "umount -n -l %s" % mountpoint
                self.root_log.warning("Forcibly unmounting '%s' from chroot." % mountpoint)
                util.do(cmd, raiseExc=0, shell=True, env=self.env)