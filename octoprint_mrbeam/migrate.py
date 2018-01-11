import os
import re
import shutil
import subprocess
from distutils.version import StrictVersion
from octoprint_mrbeam.mrb_logger import mrb_logger
from .profile import laserCutterProfileManager, InvalidProfileError, CouldNotOverwriteError, Profile


def migrate(plugin):
	Migration(plugin).run()


class Migration(object):

	VERSION_DELETE_EGG_DIR_LEFTOVERS = '0.1.17'
	VERSION_SETUP_IPTABLES           = '0.1.19'

	def __init__(self, plugin):
		self._logger = mrb_logger("octoprint.plugins.mrbeam.migrate")
		self.plugin = plugin

		self.version_previous = self.plugin._settings.get(['version'])
		self.version_current  = self.plugin._plugin_version


	def run(self):
		try:
			if not self.is_lasercutterProfile_set(): self.set_lasercutterProfile()

			if self.is_migration_required():
				self._logger.info("Starting migration from v{} to v{}".format(self.version_previous, self.version_current))

				# migrations
				if self.version_previous is None or self._compare_versions(self.version_previous, '0.1.13', equal_ok=False):
					self.migrate_from_0_0_0()

				if self.version_previous is None or self._compare_versions(self.version_previous, self.VERSION_DELETE_EGG_DIR_LEFTOVERS, equal_ok=False):
					self.delete_egg_dir_leftovers()

				if self.version_previous is None or self._compare_versions(self.version_previous, self.VERSION_SETUP_IPTABLES, equal_ok=False):
					self.setup_iptables()
				# migrations end

				self.save_current_version()
			else:
				self._logger.debug("No migration required.")
		except:
			self._logger.exception("Unhandled exception during migration: ")


	def is_migration_required(self):
		if self.version_previous is None:
			return True
		try:
			StrictVersion(self.version_previous)
		except ValueError as e:
			self._logger.error("Previous version is invalid: '{}'. ValueError from StrictVersion: {}".format(self.version_previous, e))
			return None
		return StrictVersion(self.version_current) > StrictVersion(self.version_previous)

	def _compare_versions(self, lower_vers, higher_vers, equal_ok=True):
		"""
		Compares two versions and returns true if lower_vers < higher_vers
		:param lower_vers: needs to be inferior to higher_vers to be True
		:param lower_vers: needs to be superior to lower_vers to be True
		:param equal_ok: returned value if lower_vers and lower_vers are equal.
		:return: True or False. None if one of the version was not a valid version number
		"""
		if lower_vers is None or higher_vers is None:
			return None
		try:
			StrictVersion(lower_vers)
			StrictVersion(higher_vers)
		except ValueError as e:
			self._logger.error("_compare_versions() One of the two version is invalid: lower_vers:{}, higher_vers:{}. ValueError from StrictVersion: {}".format(lower_vers, higher_vers, e))
			return None
		if StrictVersion(lower_vers) == StrictVersion(higher_vers):
			return equal_ok
		return StrictVersion(lower_vers) < StrictVersion(higher_vers)

	def save_current_version(self):
		self.plugin._settings.set(['version'], self.version_current, force=True)


	def migrate_from_0_0_0(self):
		self._logger.info("migrate_from_0_0_0() ")
		my_profile = laserCutterProfileManager().get_default()
		# this setting was introduce with MrbeamPlugin version 0.1.13
		my_profile['laser']['intensity_factor'] = 13
		self._logger.info("migrate_from_0_0_0() Set lasercutterProfile ['laser']['intensity_factor'] = 13")
		# previous default was 300 (5min)
		my_profile['dust']['auto_mode_time'] = 60
		self._logger.info("migrate_from_0_0_0() Set lasercutterProfile ['dust']['auto_mode_time'] = 60")
		laserCutterProfileManager().save(my_profile, allow_overwrite=True, make_default=True)


	def delete_egg_dir_leftovers(self):
		"""
		Deletes egg files/dirs of older versions of MrBeamPlugin
		Our first mrb_check USB sticks updated MrBeamPlugin per 'pip --ignore-installed'
		which left old egg directories in site-packages.
		This then caused the plugin to assume it's version is the old version, even though the new code was executed.
		"""
		self._logger.info("delete_egg_dir_leftovers() ")
		site_packages_dir = '/home/pi/site-packages'
		if os.path.isdir(site_packages_dir):
			for f in os.listdir(site_packages_dir):
				match = re.match(r'Mr_Beam-(?P<version>[0-9.]+)[.-].+', f)
				if match:
					version = match.group('version')
					if self._compare_versions(version, self.VERSION_DELETE_EGG_DIR_LEFTOVERS, equal_ok=False):
						del_dir = os.path.join(site_packages_dir, f)
						self._logger.info("delete_egg_dir_leftovers() Deleting dir: %s", del_dir)
						shutil.rmtree(del_dir)
		else:
			self._logger.error("delete_egg_dir_leftovers() Dir not existing '%s', Can't check for egg leftovers.")


	def setup_iptables(self):
		"""
		Creates iptables config file.
		This is required to redirect all incoming traffic to localhost.
		"""
		self._logger.info("setup_iptables() ")
		iptables_file = '/etc/network/if-up.d/iptables'
		iptables_body = """#!/bin/sh
iptables -t nat -F
# route all incoming traffic to localhost
sysctl -w net.ipv4.conf.all.route_localnet=1
iptables -t nat -I PREROUTING -p tcp --dport 80 -j DNAT --to 127.0.0.1:80
"""

		command = ['sudo', 'bash', '-c', "echo '{data}' > {file}".format(data=iptables_body, file=iptables_file)]
		out, code = self._exec_cmd_output(command)
		if code != 0:
			self._logger.error("setup_iptables() Error while writing iptables conf: '%s'", out)
			return

		command = ['sudo', 'chmod', '+x', iptables_file]
		out, code = self._exec_cmd_output(command)
		if code != 0:
			self._logger.error("setup_iptables() Error while chmod iptables conf: '%s'", out)
			return

		command = ['sudo', 'bash', '-c', iptables_file]
		out, code = self._exec_cmd_output(command)
		if code != 0:
			self._logger.error("setup_iptables() Error while executing iptables conf: '%s'", out)
			return

		self._logger.info("setup_iptables() Created and loaded iptables conf: '%s'", iptables_file)





	##########################################################
	#####             lasercutterProfiles                #####
	##########################################################

	def is_lasercutterProfile_set(self):
		"""
		Is a non-generic lasercutterProfile set as default profile?
		:return: True if a non-generic lasercutterProfile is set as default
		"""
		return laserCutterProfileManager().get_default()['id'] != 'my_default'


	def set_lasercutterProfile(self):
		if laserCutterProfileManager().get_default()['id'] == 'my_default':
			self._logger.info("set_lasercutterPorfile() Setting lasercutterProfile for device '%s'", self.plugin._device_series)

			if self.plugin._device_series == '2X':
				# 2X placeholder value.
				self._logger.error("set_lasercutterProfile() Can't set lasercutterProfile. device_series is %s: ", self.plugin._device_series)
				return
			elif self.plugin._device_series == '2C':
				self.set_lasercutterPorfile_2C()
			elif self.plugin._device_series == '2D':
				self.set_lasercutterPorfile_2D()
			else:
				self.set_lasercutterPorfile_2all()
			self.save_current_version()


	def set_lasercutterPorfile_2all(self):
		profile_id = "MrBeam{}".format(self.plugin._device_series)
		if laserCutterProfileManager().exists(profile_id):
			laserCutterProfileManager().set_default(profile_id)
			self._logger.info("set_lasercutterPorfile_2all() Set lasercutterProfile '%s' as default.", profile_id)
		else:
			self._logger.warn("set_lasercutterPorfile_2all() No lasercutterProfile '%s' found. Keep using generic profile.", profile_id)


	def set_lasercutterPorfile_2C(self):
		"""
		Series C came with no default lasercutterProfile set.
		FYI: the image contained only a profile called 'MrBeam2B' which was never used since it wasn't set as default
		"""
		profile_id = "MrBeam2C"
		model = "C"

		if laserCutterProfileManager().exists(profile_id):
			laserCutterProfileManager().set_default(profile_id)
			self._logger.info("set_lasercutterPorfile_2C() Set lasercutterProfile '%s' as default.", profile_id)
		else:
			default_profile = laserCutterProfileManager().get_default()
			default_profile['id'] = profile_id
			default_profile['name'] = "MrBeam2"
			default_profile['model'] = model
			default_profile['legacy'] = dict()
			default_profile['legacy']['job_done_home_position_x'] = 250
			laserCutterProfileManager().save(default_profile, allow_overwrite=True, make_default=True)
			self._logger.info("set_lasercutterPorfile_2C() Created lasercutterProfile '%s' and set as default. Content: %s", profile_id, default_profile)


	def set_lasercutterPorfile_2D(self):
		"""
		Not sure if first D Series devices going to be shipped with a proper lasercutterProfile installed...
		In case not, let's create it.
		:return:
		"""
		profile_id = "MrBeam2D"
		model = "D"

		if laserCutterProfileManager().exists(profile_id):
			laserCutterProfileManager().set_default(profile_id)
			self._logger.info("set_lasercutterPorfile_2D() Set lasercutterProfile '%s' as default.", profile_id)
		else:
			default_profile = laserCutterProfileManager().get_default()
			default_profile['id'] = profile_id
			default_profile['name'] = "MrBeam2"
			default_profile['model'] = model
			laserCutterProfileManager().save(default_profile, allow_overwrite=True, make_default=True)
			self._logger.info("set_lasercutterPorfile_2D() Created lasercutterProfile '%s' and set as default. Content: %s",profile_id, default_profile)


	##########################################################
	#####                 helpers                        #####
	##########################################################

	def _exec_cmd_output(self, cmd):
		'''
		Executes a system command and returns its output.
		:param cmd:
		:return: Tuple(String:output , int return_code)
				If system returncode was not 0 (zero), output will be the error message
		'''
		output = None
		code = 0
		self._logger.debug("_exec_cmd_output() command: '%s'", cmd)
		try:
			output = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
		except subprocess.CalledProcessError as e:
			code = e.returncode
			output = e.output
			self._logger.debug("Fail to execute command '%s', return code: %s, output: '%s'", cmd, e.returncode, e.output)

		return output, code