import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:lucide_icons/lucide_icons.dart';
import '../../core/api_client.dart';
import '../../core/theme.dart';

final settingsDataProvider = FutureProvider.autoDispose<Map<String, dynamic>>((ref) async {
  final dio = ref.watch(apiClientProvider);
  final response = await dio.get('/api/settings');
  return Map<String, dynamic>.from(response.data as Map);
});

class SettingsScreen extends ConsumerStatefulWidget {
  const SettingsScreen({super.key});

  @override
  ConsumerState<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends ConsumerState<SettingsScreen> {
  // Personal settings controllers & state
  final _newPasswordCtrl = TextEditingController();
  bool _obscureNewPassword = true;
  final _rewindCtrl = TextEditingController();
  int? _autoPauseEpisodes;
  String? _defaultAudioLang;
  String? _defaultSubtitleLang;

  // Admin form controllers & state
  final _volumePathCtrl = TextEditingController();
  String _volumeMediaType = 'movie';
  final _tmdbCtrl = TextEditingController();
  bool _obscureTmdb = true;
  final _theaudiodbCtrl = TextEditingController();
  bool _obscureTheaudiodb = true;
  final _opensubtitlesCtrl = TextEditingController();
  bool _obscureOpensubtitles = true;
  final _remoteUrlCtrl = TextEditingController();
  bool _obscureRemoteUrl = true;

  String _hwAccel = 'auto';
  String _hdrTonemap = 'hable';
  String _autoIntro = 'true';

  // Loom controllers & state
  bool _loomEnabled = false;
  bool _loomAllowRelay = false;
  bool _loomPauseOnStreams = true;
  final _loomNodeIdCtrl = TextEditingController();
  bool _obscureLoomNodeId = true;
  String _loomSyncDir = '/media/loom';
  final _loomUploadLimitCtrl = TextEditingController();
  final _loomDownloadLimitCtrl = TextEditingController();
  String _loomMaxQuality = '1080p';
  final _loomMaxSearchHopsCtrl = TextEditingController();
  final _loomMaxSendingHopsCtrl = TextEditingController();
  final _loomMaxConcurrentCtrl = TextEditingController();
  final _loomMaxWorkersCtrl = TextEditingController();
  final _loomMaxReqMinCtrl = TextEditingController();
  final _loomMaxFileGbCtrl = TextEditingController();
  String _loomAutoRotate = 'disabled';
  final _loomAllowedExtsCtrl = TextEditingController();
  Set<int> _loomSearchableVolumeIds = {};

  bool _hydrated = false;
  String? _languageStatus;
  String? _autoPauseStatus;
  String? _transcodeStatus;

  @override
  void dispose() {
    _newPasswordCtrl.dispose();
    _rewindCtrl.dispose();
    _volumePathCtrl.dispose();
    _tmdbCtrl.dispose();
    _theaudiodbCtrl.dispose();
    _opensubtitlesCtrl.dispose();
    _remoteUrlCtrl.dispose();
    _loomNodeIdCtrl.dispose();
    _loomUploadLimitCtrl.dispose();
    _loomDownloadLimitCtrl.dispose();
    _loomMaxSearchHopsCtrl.dispose();
    _loomMaxSendingHopsCtrl.dispose();
    _loomMaxConcurrentCtrl.dispose();
    _loomMaxWorkersCtrl.dispose();
    _loomMaxReqMinCtrl.dispose();
    _loomMaxFileGbCtrl.dispose();
    _loomAllowedExtsCtrl.dispose();
    super.dispose();
  }

  void _hydrateFromPayload(Map<String, dynamic> data) {
    if (_hydrated) return;
    _hydrated = true;

    final prefs = Map<String, dynamic>.from(data['preferences'] as Map? ?? {});
    _rewindCtrl.text = '${prefs['rewind_seconds'] ?? 0}';
    _autoPauseEpisodes = prefs['auto_pause_episodes'] as int? ?? 3;
    _defaultAudioLang = prefs['default_audio_lang'] as String? ?? 'eng';
    _defaultSubtitleLang = prefs['default_subtitle_lang'] as String? ?? 'off';

    if (data['admin'] is Map) {
      final admin = Map<String, dynamic>.from(data['admin'] as Map);
      _tmdbCtrl.text = admin['tmdb_key']?.toString() ?? '';
      _theaudiodbCtrl.text = admin['theaudiodb_key']?.toString() ?? '';
      _opensubtitlesCtrl.text = admin['opensubtitles_key']?.toString() ?? '';
      _remoteUrlCtrl.text = admin['remote_url']?.toString() ?? '';
      final rawHw = admin['hw_accel_profile']?.toString() ?? 'auto';
      _hwAccel = const ['auto', 'software', 'nvenc', 'qsv', 'vaapi', 'videotoolbox'].contains(rawHw) ? rawHw : 'auto';
      final rawHdr = admin['hdr_tonemapping']?.toString() ?? 'hable';
      _hdrTonemap = const ['disabled', 'hable', 'mobius', 'reinhard', 'linear', 'gamma', 'clip'].contains(rawHdr) ? rawHdr : 'hable';
      final rawIntro = admin['auto_intro_detection']?.toString().toLowerCase() ?? 'true';
      _autoIntro = (rawIntro == 'false' || rawIntro == 'off' || rawIntro == '0') ? 'false' : 'true';

      final loom = admin['loom_settings'] is Map ? Map<String, dynamic>.from(admin['loom_settings'] as Map) : const {};
      _loomEnabled = loom['enabled'] == true;
      _loomAllowRelay = loom['allow_relay'] == true;
      _loomPauseOnStreams = loom['pause_on_active_streams'] != false;
      _loomNodeIdCtrl.text = loom['node_id']?.toString() ?? '';
      _loomSyncDir = (loom['sync_dir']?.toString().isNotEmpty == true) ? loom['sync_dir'].toString() : '/media/loom';
      _loomUploadLimitCtrl.text = '${loom['upload_bandwidth_limit_kbps'] ?? 0}';
      _loomDownloadLimitCtrl.text = '${loom['download_bandwidth_limit_kbps'] ?? 0}';
      _loomMaxQuality = loom['max_quality']?.toString() ?? '1080p';
      _loomMaxSearchHopsCtrl.text = '${loom['max_search_hops'] ?? 1}';
      _loomMaxSendingHopsCtrl.text = '${loom['max_sending_hops'] ?? 1}';
      _loomMaxConcurrentCtrl.text = '${loom['max_concurrent_transfers'] ?? 2}';
      _loomMaxWorkersCtrl.text = '${loom['max_global_workers'] ?? 3}';
      _loomMaxReqMinCtrl.text = '${loom['max_requests_per_minute'] ?? 3}';
      _loomMaxFileGbCtrl.text = '${loom['max_file_size_gb'] ?? 0}';
      _loomAutoRotate = loom['auto_rotate_interval']?.toString() ?? 'disabled';
      _loomAllowedExtsCtrl.text = loom['allowed_filetypes']?.toString() ?? '.mp4, .mkv, .mp3, .jpg, .png';
      final rawVolIds = loom['searchable_volume_ids'] as List? ?? [];
      _loomSearchableVolumeIds = rawVolIds.map((e) => int.tryParse('$e') ?? -1).where((id) => id > 0).toSet();
    }
  }

  Future<void> _reloadSettings() async {
    _hydrated = false;
    final _ = await ref.refresh(settingsDataProvider.future);
  }

  void _snack(String msg, {bool isError = false}) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(msg),
        backgroundColor: isError ? AppColors.failure : AppColors.surface,
      ),
    );
  }

  // Matches password-strength.js
  (double, String, Color) _evaluatePasswordStrength(String pw) {
    if (pw.isEmpty) return (0.0, '', Colors.transparent);
    int score = 0;
    if (pw.length >= 6) score++;
    if (pw.length >= 10) score++;
    if (RegExp(r'[A-Z]').hasMatch(pw) && RegExp(r'[a-z]').hasMatch(pw)) score++;
    if (RegExp(r'[0-9]').hasMatch(pw)) score++;
    if (RegExp(r'[^A-Za-z0-9]').hasMatch(pw)) score++;
    if (pw.length < 6 || score <= 2) return (0.33, 'Weak', AppColors.strengthWeak);
    if (score <= 3) return (0.66, 'Good', AppColors.strengthGood);
    return (1.0, 'Great', AppColors.strengthGreat);
  }

  Future<void> _changePassword() async {
    final pwd = _newPasswordCtrl.text.trim();
    if (pwd.length < 6) {
      _snack('Password must be at least 6 characters.', isError: true);
      return;
    }
    try {
      await ref.read(apiClientProvider).post('/api/settings', data: {'new_password': pwd});
      _newPasswordCtrl.clear();
      setState(() {});
      _snack('Password updated');
    } catch (e) {
      _snack(formatApiError(e, fallback: 'Failed to change password.'), isError: true);
    }
  }

  Future<void> _saveRewind() async {
    final secs = int.tryParse(_rewindCtrl.text.trim()) ?? 0;
    try {
      await ref.read(apiClientProvider).post('/api/settings', data: {'rewind_seconds': secs.clamp(0, 300)});
      _snack('Rewind setting saved');
    } catch (e) {
      _snack(formatApiError(e, fallback: 'Failed to save rewind.'), isError: true);
    }
  }

  Future<void> _autoSaveLanguages() async {
    try {
      await ref.read(apiClientProvider).post('/api/settings', data: {
        'default_audio_lang': _defaultAudioLang ?? 'eng',
        'default_subtitle_lang': _defaultSubtitleLang ?? 'off',
      });
      if (!mounted) return;
      setState(() => _languageStatus = 'Saved');
      Future.delayed(const Duration(seconds: 2), () {
        if (mounted) setState(() => _languageStatus = null);
      });
    } catch (e) {
      _snack(formatApiError(e, fallback: 'Failed to save languages.'), isError: true);
    }
  }

  Future<void> _autoSaveAutoPause() async {
    try {
      await ref.read(apiClientProvider).post('/api/settings', data: {
        'auto_pause_episodes': _autoPauseEpisodes ?? 3,
      });
      if (!mounted) return;
      setState(() => _autoPauseStatus = 'Saved');
      Future.delayed(const Duration(seconds: 2), () {
        if (mounted) setState(() => _autoPauseStatus = null);
      });
    } catch (e) {
      _snack(formatApiError(e, fallback: 'Failed to save auto-pause.'), isError: true);
    }
  }

  Future<void> _autoSaveTranscoding() async {
    try {
      await ref.read(apiClientProvider).post(
            '/settings/transcoding',
            data: FormData.fromMap({
              'hw_accel_profile': _hwAccel,
              'hdr_tonemapping': _hdrTonemap,
              'auto_intro_detection': _autoIntro,
            }),
          );
      if (!mounted) return;
      setState(() => _transcodeStatus = 'Saved');
      Future.delayed(const Duration(seconds: 2), () {
        if (mounted) setState(() => _transcodeStatus = null);
      });
    } catch (e) {
      _snack(formatApiError(e, fallback: 'Failed to save transcoding settings.'), isError: true);
    }
  }

  Future<void> _saveFormEndpoint(String endpoint, Map<String, dynamic> fields, String label) async {
    try {
      await ref.read(apiClientProvider).post(endpoint, data: FormData.fromMap(fields));
      _snack('$label saved');
      await _reloadSettings();
    } catch (e) {
      _snack(formatApiError(e, fallback: 'Failed to save $label.'), isError: true);
    }
  }

  Future<void> _saveLoomSettings() async {
    try {
      await ref.read(apiClientProvider).post('/api/loom/settings', data: {
        'enabled': _loomEnabled,
        'allow_relay': _loomAllowRelay,
        'pause_on_active_streams': _loomPauseOnStreams,
        'sync_dir': _loomSyncDir,
        'upload_bandwidth_limit_kbps': int.tryParse(_loomUploadLimitCtrl.text.trim()) ?? 0,
        'download_bandwidth_limit_kbps': int.tryParse(_loomDownloadLimitCtrl.text.trim()) ?? 0,
        'max_quality': _loomMaxQuality,
        'max_search_hops': (int.tryParse(_loomMaxSearchHopsCtrl.text.trim()) ?? 1).clamp(1, 10),
        'max_sending_hops': (int.tryParse(_loomMaxSendingHopsCtrl.text.trim()) ?? 1).clamp(1, 10),
        'max_concurrent_transfers': (int.tryParse(_loomMaxConcurrentCtrl.text.trim()) ?? 2).clamp(1, 20),
        'max_global_workers': (int.tryParse(_loomMaxWorkersCtrl.text.trim()) ?? 3).clamp(1, 12),
        'max_requests_per_minute': (int.tryParse(_loomMaxReqMinCtrl.text.trim()) ?? 3).clamp(1, 120),
        'max_file_size_gb': double.tryParse(_loomMaxFileGbCtrl.text.trim()) ?? 0.0,
        'auto_rotate_interval': _loomAutoRotate,
        'allowed_filetypes': _loomAllowedExtsCtrl.text.trim(),
        'searchable_volume_ids': _loomSearchableVolumeIds.toList(),
      });
      _snack('Loom settings saved');
      await _reloadSettings();
    } catch (e) {
      _snack(formatApiError(e, fallback: 'Failed to save Loom settings.'), isError: true);
    }
  }

  Future<void> _openDirectoryBrowser() async {
    final dio = ref.read(apiClientProvider);
    String current = '/';
    List<String> dirs = [];
    bool loading = true;

    Future<void> fetch(String path, StateSetter setModalState) async {
      setModalState(() => loading = true);
      try {
        final resp = await dio.get('/api/browse', queryParameters: {'path': path});
        current = resp.data['current']?.toString() ?? path;
        dirs = (resp.data['directories'] as List? ?? []).map((e) => e.toString()).toList();
      } catch (_) {
        dirs = [];
      } finally {
        setModalState(() => loading = false);
      }
    }

    await showDialog(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (sbCtx, setModalState) {
          if (loading && dirs.isEmpty) {
            Future.microtask(() => fetch(current, setModalState));
          }
          return AlertDialog(
            title: const Text('Select Directory'),
            content: SizedBox(
              width: 360,
              height: 360,
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(current, style: const TextStyle(color: AppColors.textMuted, fontSize: 12)),
                  const SizedBox(height: 10),
                  Expanded(
                    child: loading
                        ? const Center(child: CircularProgressIndicator(color: AppColors.accent))
                        : ListView.builder(
                            itemCount: dirs.length,
                            itemBuilder: (c, i) {
                              final d = dirs[i];
                              return ListTile(
                                dense: true,
                                leading: const Icon(LucideIcons.folder, size: 16, color: AppColors.textMuted),
                                title: Text(d),
                                onTap: () {
                                  if (d == '..') {
                                    final parts = current.split('/').where((p) => p.isNotEmpty).toList();
                                    if (parts.isNotEmpty) parts.removeLast();
                                    fetch('/${parts.join('/')}', setModalState);
                                  } else {
                                    final next = current.endsWith('/') ? '$current$d' : '$current/$d';
                                    fetch(next, setModalState);
                                  }
                                },
                              );
                            },
                          ),
                  ),
                ],
              ),
            ),
            actions: [
              TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('Cancel')),
              ElevatedButton(
                onPressed: () {
                  _volumePathCtrl.text = current;
                  Navigator.pop(ctx);
                },
                child: const Text('Use This Folder'),
              ),
            ],
          );
        },
      ),
    );
  }

  Future<void> _updatePermissionEntity({
    required String endpoint,
    required Map<String, dynamic> entity,
    required String field,
  }) async {
    final nextVal = !(entity[field] == true);
    entity[field] = nextVal;
    setState(() {});
    final formMap = <String, dynamic>{};
    for (final key in [
      'movies_access',
      'tv_access',
      'photos_access',
      'music_access',
      'downloads_access',
      'watch_together_access',
      'can_manage_profiles',
    ]) {
      if (entity[key] == true) formMap[key] = 'on';
    }
    try {
      await ref.read(apiClientProvider).post(endpoint, data: FormData.fromMap(formMap));
    } catch (e) {
      entity[field] = !nextVal;
      if (mounted) setState(() {});
      _snack(formatApiError(e, fallback: 'Failed to update permission.'), isError: true);
    }
  }

  @override
  Widget build(BuildContext context) {
    final settingsAsync = ref.watch(settingsDataProvider);
    final session = ref.watch(appSessionProvider);

    return Scaffold(
      backgroundColor: AppColors.bg,
      appBar: AppBar(
        backgroundColor: AppColors.bg,
        leading: IconButton(
          icon: const Icon(LucideIcons.arrowLeft, color: AppColors.textMuted),
          onPressed: () => context.pop(),
        ),
        title: const Text(
          'Settings',
          style: TextStyle(color: AppColors.text, fontSize: 24, fontWeight: FontWeight.bold),
        ),
      ),
      body: settingsAsync.when(
        loading: () => const Center(child: CircularProgressIndicator(color: AppColors.accent)),
        error: (err, _) => Center(
          child: Padding(
            padding: const EdgeInsets.all(24.0),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(formatApiError(err, fallback: 'Unable to load settings.'), textAlign: TextAlign.center, style: const TextStyle(color: AppColors.failure)),
                const SizedBox(height: 16),
                ElevatedButton.icon(
                  onPressed: _reloadSettings,
                  icon: const Icon(LucideIcons.refreshCw, size: 16),
                  label: const Text('Retry'),
                ),
              ],
            ),
          ),
        ),
        data: (data) {
          _hydrateFromPayload(data);
          final isAdmin = data['is_admin'] == true;
          final canChangePassword = data['can_change_password'] != false;
          final user = Map<String, dynamic>.from(data['user'] as Map? ?? {});
          final activeProfile = Map<String, dynamic>.from(data['active_profile'] as Map? ?? {});
          final availableLangs = (data['available_languages'] as List? ?? [])
              .map((e) => Map<String, dynamic>.from(e as Map))
              .toList();
          final server = Map<String, dynamic>.from(data['server'] as Map? ?? {});
          final admin = data['admin'] is Map ? Map<String, dynamic>.from(data['admin'] as Map) : null;
          final isSecondaryProfile = activeProfile['id'] != null && activeProfile['is_primary'] != true;
          final profSuffix = isSecondaryProfile ? ' (${activeProfile['name']})' : '';

          return RefreshIndicator(
            onRefresh: _reloadSettings,
            child: ListView(
              padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 12),
              children: [
                if (canChangePassword) ...[
                  _sectionHeader('My Account, ${user['username'] ?? ''}'),
                  _buildPasswordInput(
                    controller: _newPasswordCtrl,
                    hint: 'New Password',
                    obscure: _obscureNewPassword,
                    onToggleObscure: () => setState(() => _obscureNewPassword = !_obscureNewPassword),
                    onChanged: (_) => setState(() {}),
                  ),
                  _buildPasswordStrengthBar(),
                  const SizedBox(height: 10),
                  SizedBox(
                    width: double.infinity,
                    child: ElevatedButton.icon(
                      onPressed: _changePassword,
                      icon: const Icon(LucideIcons.rotateCcw, size: 16),
                      label: const Text('Change Password'),
                    ),
                  ),
                  const SizedBox(height: 28),
                ],

                _sectionHeader('Rewind on Resume$profSuffix'),
                const Text(
                  'Set how many seconds playback will automatically rewind when opening a movie or episode with saved progress. Set to 0 to disable.',
                  style: TextStyle(color: AppColors.textMuted, fontSize: 13),
                ),
                const SizedBox(height: 12),
                TextField(
                  controller: _rewindCtrl,
                  keyboardType: TextInputType.number,
                  decoration: const InputDecoration(hintText: 'Seconds (e.g. 5, 10)'),
                ),
                const SizedBox(height: 10),
                SizedBox(
                  width: double.infinity,
                  child: ElevatedButton.icon(
                    onPressed: _saveRewind,
                    icon: const Icon(LucideIcons.save, size: 16),
                    label: const Text('Save'),
                  ),
                ),
                const SizedBox(height: 28),

                _sectionHeader('Audio & Subtitle Preferences$profSuffix', statusText: _languageStatus),
                const Text(
                  'Choose the default audio and subtitle languages for video playback.',
                  style: TextStyle(color: AppColors.textMuted, fontSize: 13),
                ),
                const SizedBox(height: 12),
                const Text('Default Audio Language', style: TextStyle(color: AppColors.textMuted, fontWeight: FontWeight.w600, fontSize: 13)),
                const SizedBox(height: 6),
                DropdownButtonFormField<String>(
                  value: availableLangs.any((l) => l['code'] == _defaultAudioLang) ? _defaultAudioLang : 'eng',
                  dropdownColor: AppColors.surface,
                  items: availableLangs
                      .map((l) => DropdownMenuItem<String>(value: l['code'] as String, child: Text(l['name'] as String)))
                      .toList(),
                  onChanged: (val) {
                    if (val != null) {
                      setState(() => _defaultAudioLang = val);
                      _autoSaveLanguages();
                    }
                  },
                ),
                const SizedBox(height: 14),
                const Text('Default Subtitle Language', style: TextStyle(color: AppColors.textMuted, fontWeight: FontWeight.w600, fontSize: 13)),
                const SizedBox(height: 6),
                DropdownButtonFormField<String>(
                  value: (_defaultSubtitleLang == 'off' || availableLangs.any((l) => l['code'] == _defaultSubtitleLang))
                      ? _defaultSubtitleLang
                      : 'off',
                  dropdownColor: AppColors.surface,
                  items: [
                    const DropdownMenuItem<String>(value: 'off', child: Text('Off')),
                    ...availableLangs.map((l) => DropdownMenuItem<String>(value: l['code'] as String, child: Text(l['name'] as String))),
                  ],
                  onChanged: (val) {
                    if (val != null) {
                      setState(() => _defaultSubtitleLang = val);
                      _autoSaveLanguages();
                    }
                  },
                ),
                const SizedBox(height: 28),

                _sectionHeader('Playback & Auto-Pause$profSuffix', statusText: _autoPauseStatus),
                const Text(
                  'Automatically pause consecutive TV show playback when inactive to prevent runaway overnight streaming on TVs and mobile devices.',
                  style: TextStyle(color: AppColors.textMuted, fontSize: 13),
                ),
                const SizedBox(height: 12),
                const Text('"Are You Still Watching?" Prompt', style: TextStyle(color: AppColors.textMuted, fontWeight: FontWeight.w600, fontSize: 13)),
                const SizedBox(height: 6),
                DropdownButtonFormField<int>(
                  value: _autoPauseEpisodes ?? 3,
                  dropdownColor: AppColors.surface,
                  items: const [
                    DropdownMenuItem(value: 2, child: Text('After 2 episodes')),
                    DropdownMenuItem(value: 3, child: Text('After 3 episodes (Recommended)')),
                    DropdownMenuItem(value: 4, child: Text('After 4 episodes')),
                    DropdownMenuItem(value: 5, child: Text('After 5 episodes')),
                    DropdownMenuItem(value: 0, child: Text('Never (Continuous Playback)')),
                  ],
                  onChanged: (val) {
                    if (val != null) {
                      setState(() => _autoPauseEpisodes = val);
                      _autoSaveAutoPause();
                    }
                  },
                ),
                const SizedBox(height: 28),

                if (isAdmin && admin != null) ..._buildAdminSections(admin),

                _sectionHeader('Device & Session'),
                Container(
                  padding: const EdgeInsets.all(14),
                  decoration: BoxDecoration(
                    color: AppColors.surface,
                    borderRadius: BorderRadius.circular(AppSpacing.radius),
                  ),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        '${session.activeServer?.name ?? 'Heirloom'} • ${session.serverUrl ?? ''}',
                        style: const TextStyle(color: AppColors.text, fontWeight: FontWeight.w600, fontSize: 14),
                      ),
                      const SizedBox(height: 4),
                      Text(
                        'Server v${server['version'] ?? AppTheme.appVersion} • ${session.savedServers.length} saved server(s) • Keep signed in: ${session.rememberMe ? 'On' : 'Off'}',
                        style: const TextStyle(color: AppColors.textMuted, fontSize: 12),
                      ),
                      const SizedBox(height: 12),
                      SizedBox(
                        width: double.infinity,
                        child: ElevatedButton.icon(
                          style: AppButtonStyles.secondary,
                          onPressed: () => context.push('/profiles'),
                          icon: const Icon(LucideIcons.users, size: 15),
                          label: const Text('Switch Profile'),
                        ),
                      ),
                      const SizedBox(height: 8),
                      SizedBox(
                        width: double.infinity,
                        child: ElevatedButton.icon(
                          style: AppButtonStyles.secondary,
                          onPressed: () => context.push('/setup'),
                          icon: const Icon(LucideIcons.server, size: 15),
                          label: const Text('Switch Server'),
                        ),
                      ),
                      const SizedBox(height: 8),
                      SizedBox(
                        width: double.infinity,
                        child: ElevatedButton.icon(
                          style: AppButtonStyles.danger,
                          onPressed: () async {
                            final router = GoRouter.of(context);
                            await ref.read(appSessionProvider.notifier).clearAllSavedCredentials();
                            router.go('/login');
                          },
                          icon: const Icon(LucideIcons.trash2, size: 15),
                          label: const Text('Clear Saved Credentials'),
                        ),
                      ),
                    ],
                  ),
                ),
                const SizedBox(height: 16),
                SizedBox(
                  width: double.infinity,
                  child: ElevatedButton.icon(
                    style: AppButtonStyles.danger,
                    onPressed: _confirmSignOut,
                    icon: const Icon(LucideIcons.logOut, size: 16),
                    label: const Text('Sign Out'),
                  ),
                ),
                const SizedBox(height: 16),
                const Center(
                  child: Text(
                    'Heirloom v${AppTheme.appVersion}',
                    style: TextStyle(color: AppColors.textMuted, fontSize: 12),
                  ),
                ),
                const SizedBox(height: 32),
              ],
            ),
          );
        },
      ),
    );
  }

  List<Widget> _buildAdminSections(Map<String, dynamic> admin) {
    final volumePaths = (admin['volume_paths'] as List? ?? []).whereType<Map>().map((e) => Map<String, dynamic>.from(e)).toList();
    final mountedVolumes = Map<String, dynamic>.from(admin['mounted_volumes'] as Map? ?? {});
    final users = (admin['users'] as List? ?? []).whereType<Map>().map((e) => Map<String, dynamic>.from(e)).toList();
    final invites = (admin['invites'] as List? ?? []).whereType<Map>().map((e) => Map<String, dynamic>.from(e)).toList();
    final hostUrl = admin['host_url']?.toString() ?? '';
    final loomPeers = (admin['loom_peers'] as List? ?? []).whereType<Map>().map((e) => Map<String, dynamic>.from(e)).toList();
    final loomJobs = (admin['loom_jobs'] as List? ?? []).whereType<Map>().map((e) => Map<String, dynamic>.from(e)).toList();

    return [
      // 1. MEDIA VOLUME PATHS
      _sectionHeader(
        'Media Volume Paths',
        trailing: ElevatedButton.icon(
          style: AppButtonStyles.secondary,
          onPressed: () => _saveFormEndpoint('/settings/rescan', const {}, 'Library rescan started'),
          icon: const Icon(LucideIcons.refreshCw, size: 14),
          label: const Text('Rescan Library', style: TextStyle(fontSize: 12)),
        ),
      ),
      TextField(
        controller: _volumePathCtrl,
        decoration: const InputDecoration(hintText: '/media/movies or custom volume path'),
      ),
      const SizedBox(height: 10),
      SizedBox(
        width: double.infinity,
        child: ElevatedButton.icon(
          style: AppButtonStyles.secondary,
          onPressed: _openDirectoryBrowser,
          icon: const Icon(LucideIcons.folder, size: 16),
          label: const Text('Browse'),
        ),
      ),
      const SizedBox(height: 10),
      DropdownButtonFormField<String>(
        value: _volumeMediaType,
        dropdownColor: AppColors.surface,
        items: const [
          DropdownMenuItem(value: 'movie', child: Text('Movies')),
          DropdownMenuItem(value: 'tv', child: Text('TV Shows')),
          DropdownMenuItem(value: 'photo', child: Text('Photos')),
          DropdownMenuItem(value: 'music', child: Text('Music')),
        ],
        onChanged: (v) {
          if (v != null) setState(() => _volumeMediaType = v);
        },
      ),
      const SizedBox(height: 10),
      SizedBox(
        width: double.infinity,
        child: ElevatedButton.icon(
          onPressed: () {
            final p = _volumePathCtrl.text.trim();
            if (p.isEmpty) {
              _snack('Enter a volume path first.', isError: true);
              return;
            }
            _volumePathCtrl.clear();
            _saveFormEndpoint('/settings/add_volume', {'path': p, 'media_type': _volumeMediaType}, 'Volume path');
          },
          icon: const Icon(LucideIcons.folderPlus, size: 16),
          label: const Text('Add Path'),
        ),
      ),
      const SizedBox(height: 14),
      if (volumePaths.isEmpty)
        const Text('No volume paths configured.', style: TextStyle(color: AppColors.textMuted))
      else
        ...volumePaths.map((vp) {
          final exists = vp['exists'] == true;
          return Container(
            margin: const EdgeInsets.only(bottom: 12),
            padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
            decoration: BoxDecoration(
              color: AppColors.surface,
              borderRadius: BorderRadius.circular(AppSpacing.radius),
            ),
            child: Row(
              children: [
                Expanded(
                  child: Wrap(
                    spacing: 10,
                    runSpacing: 6,
                    crossAxisAlignment: WrapCrossAlignment.center,
                    children: [
                      Container(
                        padding: const EdgeInsets.symmetric(horizontal: 7, vertical: 3),
                        decoration: BoxDecoration(
                          color: (exists ? AppColors.success : AppColors.failure).withOpacity(0.18),
                          borderRadius: BorderRadius.circular(4),
                        ),
                        child: Row(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            Icon(exists ? LucideIcons.check : LucideIcons.x, size: 12, color: exists ? AppColors.success : AppColors.failure),
                            const SizedBox(width: 4),
                            Text(
                              (vp['media_type'] ?? '').toString().toUpperCase(),
                              style: TextStyle(color: exists ? AppColors.success : AppColors.failure, fontSize: 11, fontWeight: FontWeight.bold),
                            ),
                          ],
                        ),
                      ),
                      Text(
                        vp['display_path']?.toString() ?? '',
                        style: const TextStyle(color: AppColors.text, fontWeight: FontWeight.bold, fontSize: 13),
                      ),
                    ],
                  ),
                ),
                const SizedBox(width: 8),
                ElevatedButton.icon(
                  style: AppButtonStyles.danger,
                  onPressed: () => _saveFormEndpoint('/settings/delete_volume/${vp['id']}', const {}, 'Volume deleted'),
                  icon: const Icon(LucideIcons.trash2, size: 14),
                  label: const Text('Delete', style: TextStyle(fontSize: 12)),
                ),
              ],
            ),
          );
        }),
      const SizedBox(height: 28),

      // 2. USERS & INVITATIONS
      _sectionHeader(
        'Users & Invitations',
        trailing: ElevatedButton.icon(
          onPressed: () => _saveFormEndpoint(
            '/settings/generate_invite',
            {'remote_url': _remoteUrlCtrl.text.trim()},
            'Invitation generated',
          ),
          icon: const Icon(LucideIcons.userPlus, size: 14),
          label: const Text('Add User', style: TextStyle(fontSize: 12)),
        ),
      ),
      ...users.map((u) {
        final userAdmin = u['is_admin'] == true;
        return Container(
          margin: const EdgeInsets.only(bottom: 12),
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 14),
          decoration: BoxDecoration(
            color: AppColors.surface,
            borderRadius: BorderRadius.circular(AppSpacing.radius),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Row(
                mainAxisAlignment: MainAxisAlignment.spaceBetween,
                children: [
                  Row(
                    children: [
                      Text(u['username']?.toString() ?? '', style: const TextStyle(color: AppColors.text, fontWeight: FontWeight.bold, fontSize: 15)),
                      if (userAdmin)
                        const Text('  (Admin)', style: TextStyle(color: AppColors.textMuted, fontSize: 13)),
                    ],
                  ),
                  if (!userAdmin)
                    ElevatedButton.icon(
                      style: AppButtonStyles.danger,
                      onPressed: () => _saveFormEndpoint('/settings/delete_user/${u['id']}', const {}, 'User deleted'),
                      icon: const Icon(LucideIcons.trash2, size: 14),
                      label: const Text('Delete', style: TextStyle(fontSize: 12)),
                    ),
                ],
              ),
              if (!userAdmin) ...[
                const SizedBox(height: 12),
                ..._buildPermissionPills(
                  entity: u,
                  mountedVolumes: mountedVolumes,
                  endpoint: '/settings/update_user/${u['id']}',
                ),
              ],
            ],
          ),
        );
      }),
      ...invites.map((inv) {
        final inviteUrl = '$hostUrl/register?invite=${inv['code']}';
        return Container(
          margin: const EdgeInsets.only(bottom: 12),
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 14),
          decoration: BoxDecoration(
            color: AppColors.surface,
            borderRadius: BorderRadius.circular(AppSpacing.radius),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const Text('Pending User Invitation', style: TextStyle(color: AppColors.text, fontWeight: FontWeight.bold, fontSize: 15)),
              const SizedBox(height: 4),
              Text(
                'Expires ${inv['expires_at'] ?? ''} (Code: ${inv['code']})',
                style: const TextStyle(color: AppColors.textMuted, fontSize: 12),
              ),
              const SizedBox(height: 10),
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: [
                  ElevatedButton.icon(
                    style: AppButtonStyles.secondary,
                    onPressed: () async {
                      await Clipboard.setData(ClipboardData(text: inviteUrl));
                      _snack('Invite link copied to clipboard');
                    },
                    icon: const Icon(LucideIcons.copy, size: 14),
                    label: const Text('Copy Link', style: TextStyle(fontSize: 12)),
                  ),
                  ElevatedButton.icon(
                    style: AppButtonStyles.danger,
                    onPressed: () => _saveFormEndpoint('/settings/delete_invite/${inv['id']}', const {}, 'Invitation deleted'),
                    icon: const Icon(LucideIcons.trash2, size: 14),
                    label: const Text('Delete', style: TextStyle(fontSize: 12)),
                  ),
                ],
              ),
              const SizedBox(height: 12),
              ..._buildPermissionPills(
                entity: inv,
                mountedVolumes: mountedVolumes,
                endpoint: '/settings/update_invite/${inv['id']}',
              ),
            ],
          ),
        );
      }),
      const SizedBox(height: 28),

      // 3. METADATA INTEGRATIONS & SUBTITLES
      _sectionHeader('Metadata Integration (TMDB)'),
      const Text(
        'Provide a free TMDB API key to automatically enrich your library with artwork, cast, crew, and metadata.',
        style: TextStyle(color: AppColors.textMuted, fontSize: 13),
      ),
      if (_tmdbCtrl.text.isNotEmpty) ...[
        const SizedBox(height: 8),
        const Row(
          children: [
            Icon(LucideIcons.checkCircle, size: 14, color: AppColors.success),
            SizedBox(width: 6),
            Text('TMDB API Key is configured and active.', style: TextStyle(color: AppColors.success, fontSize: 13, fontWeight: FontWeight.bold)),
          ],
        ),
      ],
      const SizedBox(height: 12),
      _buildPasswordInput(
        controller: _tmdbCtrl,
        hint: 'Enter TMDB API Key',
        obscure: _obscureTmdb,
        onToggleObscure: () => setState(() => _obscureTmdb = !_obscureTmdb),
      ),
      const SizedBox(height: 10),
      SizedBox(
        width: double.infinity,
        child: ElevatedButton.icon(
          onPressed: () => _saveFormEndpoint('/settings/tmdb', {'tmdb_key': _tmdbCtrl.text.trim()}, 'TMDB key'),
          icon: const Icon(LucideIcons.save, size: 16),
          label: const Text('Save'),
        ),
      ),
      const SizedBox(height: 28),

      _sectionHeader('Metadata Integration (TheAudioDB)'),
      const Text(
        'Enrich your music library with artist biographies, banners, thumbnails, and album metadata. Defaults to free key 123.',
        style: TextStyle(color: AppColors.textMuted, fontSize: 13),
      ),
      const SizedBox(height: 8),
      Row(
        children: [
          const Icon(LucideIcons.checkCircle, size: 14, color: AppColors.success),
          const SizedBox(width: 6),
          Expanded(
            child: Text(
              _theaudiodbCtrl.text.isNotEmpty
                  ? 'Custom TheAudioDB API Key is configured and active.'
                  : 'Active: Using default free tier key (123). No configuration needed.',
              style: const TextStyle(color: AppColors.success, fontSize: 13),
            ),
          ),
        ],
      ),
      const SizedBox(height: 12),
      _buildPasswordInput(
        controller: _theaudiodbCtrl,
        hint: 'Default: 123 (Leave blank for free key)',
        obscure: _obscureTheaudiodb,
        onToggleObscure: () => setState(() => _obscureTheaudiodb = !_obscureTheaudiodb),
      ),
      const SizedBox(height: 10),
      SizedBox(
        width: double.infinity,
        child: ElevatedButton.icon(
          onPressed: () => _saveFormEndpoint('/settings/theaudiodb', {'theaudiodb_key': _theaudiodbCtrl.text.trim()}, 'TheAudioDB key'),
          icon: const Icon(LucideIcons.save, size: 16),
          label: const Text('Save'),
        ),
      ),
      const SizedBox(height: 28),

      _sectionHeader('On-Demand Subtitles (OpenSubtitles)'),
      const Text(
        'Provide a free OpenSubtitles API key to search and fetch missing subtitles directly inside the video player.',
        style: TextStyle(color: AppColors.textMuted, fontSize: 13),
      ),
      if (_opensubtitlesCtrl.text.isNotEmpty) ...[
        const SizedBox(height: 8),
        const Row(
          children: [
            Icon(LucideIcons.checkCircle, size: 14, color: AppColors.success),
            SizedBox(width: 6),
            Text('OpenSubtitles API Key is configured and active.', style: TextStyle(color: AppColors.success, fontSize: 13, fontWeight: FontWeight.bold)),
          ],
        ),
      ],
      const SizedBox(height: 12),
      _buildPasswordInput(
        controller: _opensubtitlesCtrl,
        hint: 'Enter OpenSubtitles API Key',
        obscure: _obscureOpensubtitles,
        onToggleObscure: () => setState(() => _obscureOpensubtitles = !_obscureOpensubtitles),
      ),
      const SizedBox(height: 10),
      SizedBox(
        width: double.infinity,
        child: ElevatedButton.icon(
          onPressed: () => _saveFormEndpoint('/settings/opensubtitles', {'opensubtitles_key': _opensubtitlesCtrl.text.trim()}, 'OpenSubtitles key'),
          icon: const Icon(LucideIcons.save, size: 16),
          label: const Text('Save'),
        ),
      ),
      const SizedBox(height: 28),

      // 4. NETWORK & REMOTE ACCESS
      _sectionHeader('Network & Remote Access'),
      const Text(
        'Configure your external URL and port if you use DDNS or port forwarding so invite links work outside your network.',
        style: TextStyle(color: AppColors.textMuted, fontSize: 13),
      ),
      const SizedBox(height: 12),
      _buildPasswordInput(
        controller: _remoteUrlCtrl,
        hint: 'e.g. http://my-home.duckdns.org:8080',
        obscure: _obscureRemoteUrl,
        onToggleObscure: () => setState(() => _obscureRemoteUrl = !_obscureRemoteUrl),
        onChanged: (_) => setState(() {}),
      ),
      if (_remoteUrlCtrl.text.trim().startsWith('http://')) ...[
        const SizedBox(height: 8),
        const Text(
          'Warning: You are using HTTP. HTTPS is recommended for remote access.',
          style: TextStyle(color: AppColors.strengthGood, fontSize: 12),
        ),
      ],
      const SizedBox(height: 10),
      SizedBox(
        width: double.infinity,
        child: ElevatedButton.icon(
          onPressed: () => _saveFormEndpoint('/settings/remote_access', {'remote_url': _remoteUrlCtrl.text.trim()}, 'Remote URL'),
          icon: const Icon(LucideIcons.save, size: 16),
          label: const Text('Save'),
        ),
      ),
      const SizedBox(height: 28),

      // 5. TRANSCODING & PROCESSING
      _sectionHeader('Transcoding & Processing', statusText: _transcodeStatus),
      const Text('Hardware Encoder', style: TextStyle(color: AppColors.textMuted, fontWeight: FontWeight.w600, fontSize: 13)),
      const SizedBox(height: 4),
      const Text(
        'Select a hardware encoder if your server supports it to reduce CPU usage when streaming.',
        style: TextStyle(color: AppColors.textMuted, fontSize: 12),
      ),
      const SizedBox(height: 8),
      DropdownButtonFormField<String>(
        value: _hwAccel,
        dropdownColor: AppColors.surface,
        items: const [
          DropdownMenuItem(value: 'auto', child: Text('Auto (Detect best available)')),
          DropdownMenuItem(value: 'software', child: Text('Software (CPU / libx264)')),
          DropdownMenuItem(value: 'nvenc', child: Text('NVIDIA NVENC (h264_nvenc)')),
          DropdownMenuItem(value: 'qsv', child: Text('Intel QuickSync (h264_qsv)')),
          DropdownMenuItem(value: 'vaapi', child: Text('VA-API (h264_vaapi)')),
          DropdownMenuItem(value: 'videotoolbox', child: Text('Apple VideoToolbox (h264_videotoolbox)')),
        ],
        onChanged: (v) {
          if (v != null) {
            setState(() => _hwAccel = v);
            _autoSaveTranscoding();
          }
        },
      ),
      const SizedBox(height: 16),
      const Text('Real-Time HDR to SDR Tone Mapping', style: TextStyle(color: AppColors.textMuted, fontWeight: FontWeight.w600, fontSize: 13)),
      const SizedBox(height: 8),
      DropdownButtonFormField<String>(
        value: _hdrTonemap,
        dropdownColor: AppColors.surface,
        items: const [
          DropdownMenuItem(value: 'disabled', child: Text('Disabled (Passthrough)')),
          DropdownMenuItem(value: 'hable', child: Text('Hable (Filmic - Recommended)')),
          DropdownMenuItem(value: 'mobius', child: Text('Mobius (Preserves Highlights)')),
          DropdownMenuItem(value: 'reinhard', child: Text('Reinhard (Preserves Shadows)')),
          DropdownMenuItem(value: 'linear', child: Text('Linear (Perceptually Linear)')),
          DropdownMenuItem(value: 'gamma', child: Text('Gamma (Contrast Preservation)')),
          DropdownMenuItem(value: 'clip', child: Text('Clip (Hard Clip Luminance)')),
        ],
        onChanged: (v) {
          if (v != null) {
            setState(() => _hdrTonemap = v);
            _autoSaveTranscoding();
          }
        },
      ),
      const SizedBox(height: 16),
      const Text('Automated Intro & Outro Detection', style: TextStyle(color: AppColors.textMuted, fontWeight: FontWeight.w600, fontSize: 13)),
      const SizedBox(height: 8),
      DropdownButtonFormField<String>(
        value: _autoIntro,
        dropdownColor: AppColors.surface,
        items: const [
          DropdownMenuItem(value: 'true', child: Text('On')),
          DropdownMenuItem(value: 'false', child: Text('Off')),
        ],
        onChanged: (v) {
          if (v != null) {
            setState(() => _autoIntro = v);
            _autoSaveTranscoding();
          }
        },
      ),
      const SizedBox(height: 28),

      // 6. THE LOOM (FEDERATION)
      _sectionHeader('The Loom (Federation)'),
      Wrap(
        spacing: 8,
        runSpacing: 8,
        children: [
          _togglePill(
            label: 'Pause During Streaming',
            selected: _loomPauseOnStreams,
            fullWidth: false,
            onTap: () {
              setState(() => _loomPauseOnStreams = !_loomPauseOnStreams);
              _saveLoomSettings();
            },
          ),
          _togglePill(
            label: 'Allow Relay',
            selected: _loomAllowRelay,
            fullWidth: false,
            onTap: () {
              setState(() => _loomAllowRelay = !_loomAllowRelay);
              _saveLoomSettings();
            },
          ),
          _togglePill(
            label: 'Enable The Loom',
            selected: _loomEnabled,
            fullWidth: false,
            onTap: () {
              setState(() => _loomEnabled = !_loomEnabled);
              _saveLoomSettings();
            },
          ),
        ],
      ),
      const SizedBox(height: 12),
      const Text(
        'Opt-in friend-to-friend library discovery and guarded media sync. Disabled by default.',
        style: TextStyle(color: AppColors.textMuted, fontSize: 13),
      ),
      const SizedBox(height: 14),
      const Text('Node ID', style: TextStyle(color: AppColors.textMuted, fontWeight: FontWeight.w600, fontSize: 13)),
      const SizedBox(height: 6),
      _buildPasswordInput(
        controller: _loomNodeIdCtrl,
        hint: 'Node ID',
        readOnly: true,
        obscure: _obscureLoomNodeId,
        onToggleObscure: () => setState(() => _obscureLoomNodeId = !_obscureLoomNodeId),
      ),
      const SizedBox(height: 12),
      const Text('Sync Directory', style: TextStyle(color: AppColors.textMuted, fontWeight: FontWeight.w600, fontSize: 13)),
      const SizedBox(height: 6),
      DropdownButtonFormField<String>(
        value: _buildSyncDirItems(volumePaths).any((i) => i.value == _loomSyncDir) ? _loomSyncDir : '/media/loom',
        dropdownColor: AppColors.surface,
        items: _buildSyncDirItems(volumePaths),
        onChanged: (v) {
          if (v != null) setState(() => _loomSyncDir = v);
        },
      ),
      const SizedBox(height: 12),
      _labeledField('Max Upload Bandwidth (KB/s)', _loomUploadLimitCtrl, isNumber: true),
      _labeledField('Max Download Bandwidth (KB/s)', _loomDownloadLimitCtrl, isNumber: true),
      const Text('Max Quality Cap', style: TextStyle(color: AppColors.textMuted, fontWeight: FontWeight.w600, fontSize: 13)),
      const SizedBox(height: 6),
      DropdownButtonFormField<String>(
        value: const ['720p', '1080p', 'original'].contains(_loomMaxQuality) ? _loomMaxQuality : '1080p',
        dropdownColor: AppColors.surface,
        items: const [
          DropdownMenuItem(value: '720p', child: Text('720p')),
          DropdownMenuItem(value: '1080p', child: Text('1080p')),
          DropdownMenuItem(value: 'original', child: Text('Original')),
        ],
        onChanged: (v) {
          if (v != null) setState(() => _loomMaxQuality = v);
        },
      ),
      const SizedBox(height: 12),
      _labeledField('Max Search Hops', _loomMaxSearchHopsCtrl, isNumber: true),
      _labeledField('Max Sending Hops', _loomMaxSendingHopsCtrl, isNumber: true),
      _labeledField('Max Concurrent Transfers', _loomMaxConcurrentCtrl, isNumber: true),
      _labeledField('Max Global Workers (1–12)', _loomMaxWorkersCtrl, isNumber: true),
      _labeledField('Max Requests / Min', _loomMaxReqMinCtrl, isNumber: true),
      _labeledField('Max File Size (GB)', _loomMaxFileGbCtrl, isNumber: true),
      const Text('Auto-Rotate Addresses', style: TextStyle(color: AppColors.textMuted, fontWeight: FontWeight.w600, fontSize: 13)),
      const SizedBox(height: 6),
      DropdownButtonFormField<String>(
        value: const ['disabled', 'daily', 'monthly', 'yearly'].contains(_loomAutoRotate) ? _loomAutoRotate : 'disabled',
        dropdownColor: AppColors.surface,
        items: const [
          DropdownMenuItem(value: 'disabled', child: Text('Disabled')),
          DropdownMenuItem(value: 'daily', child: Text('Daily')),
          DropdownMenuItem(value: 'monthly', child: Text('Monthly')),
          DropdownMenuItem(value: 'yearly', child: Text('Yearly')),
        ],
        onChanged: (v) {
          if (v != null) setState(() => _loomAutoRotate = v);
        },
      ),
      const SizedBox(height: 12),
      _labeledField('Allowed Filetypes', _loomAllowedExtsCtrl, hint: '.mp4, .mkv, .mp3, .jpg, .png'),
      if (volumePaths.isNotEmpty) ...[
        const Text('Searchable Volumes', style: TextStyle(color: AppColors.textMuted, fontWeight: FontWeight.w600, fontSize: 13)),
        const SizedBox(height: 6),
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: volumePaths.map((vp) {
            final vid = vp['id'] as int;
            final sel = _loomSearchableVolumeIds.contains(vid);
            return _togglePill(
              label: '${vp['media_type']}: ${vp['display_path']}',
              selected: sel,
              fullWidth: false,
              onTap: () => setState(() {
                if (sel) {
                  _loomSearchableVolumeIds.remove(vid);
                } else {
                  _loomSearchableVolumeIds.add(vid);
                }
              }),
            );
          }).toList(),
        ),
        const SizedBox(height: 14),
      ],
      SizedBox(
        width: double.infinity,
        child: ElevatedButton.icon(
          onPressed: _saveLoomSettings,
          icon: const Icon(LucideIcons.save, size: 16),
          label: const Text('Save Loom Settings'),
        ),
      ),

      if (loomPeers.isNotEmpty) ...[
        const SizedBox(height: 18),
        const Text('Paired Peers', style: TextStyle(fontSize: 15, fontWeight: FontWeight.bold, color: AppColors.text)),
        const SizedBox(height: 8),
        ...loomPeers.map((p) {
          final allowIn = p['allow_incoming'] == true;
          final allowOut = p['allow_outgoing'] == true;
          return Container(
            margin: const EdgeInsets.only(bottom: 10),
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              color: AppColors.surface,
              borderRadius: BorderRadius.circular(AppSpacing.radius),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: [
                    Expanded(
                      child: Text(
                        '${p['name'] ?? 'Peer'} (${p['status'] ?? ''})',
                        style: const TextStyle(color: AppColors.text, fontWeight: FontWeight.bold),
                      ),
                    ),
                    ElevatedButton.icon(
                      style: AppButtonStyles.danger,
                      onPressed: () => _saveFormEndpoint('/api/loom/peers/${p['id']}/delete', const {}, 'Peer removed'),
                      icon: const Icon(LucideIcons.trash2, size: 14),
                      label: const Text('Delete', style: TextStyle(fontSize: 12)),
                    ),
                  ],
                ),
                const SizedBox(height: 10),
                Wrap(
                  spacing: 8,
                  runSpacing: 8,
                  children: [
                    _togglePill(
                      label: 'Receive',
                      selected: allowIn,
                      fullWidth: false,
                      onTap: () async {
                        await ref.read(apiClientProvider).post('/api/loom/peers/${p['id']}/update', data: {'allow_incoming': !allowIn});
                        _reloadSettings();
                      },
                    ),
                    _togglePill(
                      label: 'Share',
                      selected: allowOut,
                      fullWidth: false,
                      onTap: () async {
                        await ref.read(apiClientProvider).post('/api/loom/peers/${p['id']}/update', data: {'allow_outgoing': !allowOut});
                        _reloadSettings();
                      },
                    ),
                  ],
                ),
              ],
            ),
          );
        }),
      ],

      if (loomJobs.isNotEmpty) ...[
        const SizedBox(height: 18),
        const Text('Recent Loom Transfers', style: TextStyle(fontSize: 15, fontWeight: FontWeight.bold, color: AppColors.text)),
        const SizedBox(height: 8),
        ...loomJobs.take(6).map((j) {
          final status = j['status']?.toString() ?? '';
          final active = status == 'queued' || status == 'downloading' || status.startsWith('paused');
          return Container(
            margin: const EdgeInsets.only(bottom: 8),
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              color: AppColors.surface,
              borderRadius: BorderRadius.circular(AppSpacing.radius),
            ),
            child: Row(
              children: [
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(j['title']?.toString() ?? 'Transfer', maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontWeight: FontWeight.w600)),
                      Text('$status • ${j['progress_pct'] ?? 0}%', style: const TextStyle(color: AppColors.textMuted, fontSize: 12)),
                    ],
                  ),
                ),
                if (active)
                  ElevatedButton(
                    style: AppButtonStyles.danger,
                    onPressed: () async {
                      await ref.read(apiClientProvider).post('/api/loom/jobs/${j['id']}/cancel');
                      _reloadSettings();
                    },
                    child: const Text('Cancel', style: TextStyle(fontSize: 12)),
                  ),
              ],
            ),
          );
        }),
      ],

      const SizedBox(height: 28),
    ];
  }

  List<DropdownMenuItem<String>> _buildSyncDirItems(List<Map<String, dynamic>> volumePaths) {
    final items = <DropdownMenuItem<String>>[
      const DropdownMenuItem(value: '/media/loom', child: Text('Loom: /media/loom')),
    ];
    for (final vp in volumePaths) {
      final path = vp['path']?.toString() ?? '';
      if (vp['exists'] == true && path.isNotEmpty && path != '/media/loom') {
        items.add(DropdownMenuItem(value: path, child: Text('${vp['media_type']}: $path')));
      }
    }
    if (!items.any((i) => i.value == _loomSyncDir) && _loomSyncDir.isNotEmpty) {
      items.add(DropdownMenuItem(value: _loomSyncDir, child: Text(_loomSyncDir)));
    }
    return items;
  }

  List<Widget> _buildPermissionPills({
    required Map<String, dynamic> entity,
    required Map<String, dynamic> mountedVolumes,
    required String endpoint,
  }) {
    final perms = <(String, String, bool)>[
      ('movies_access', 'Movies', mountedVolumes['movie'] == true),
      ('tv_access', 'TV Shows', mountedVolumes['tv'] == true),
      ('photos_access', 'Photos', mountedVolumes['photo'] == true),
      ('music_access', 'Music', mountedVolumes['music'] == true),
      ('downloads_access', 'Downloads', true),
      ('watch_together_access', 'Watch Together', true),
      ('can_manage_profiles', 'Household Profiles', true),
    ];

    return perms
        .where((p) => p.$3)
        .map((p) => Padding(
              padding: const EdgeInsets.only(bottom: 6),
              child: _togglePill(
                label: p.$2,
                selected: entity[p.$1] == true,
                fullWidth: true,
                onTap: () => _updatePermissionEntity(endpoint: endpoint, entity: entity, field: p.$1),
              ),
            ))
        .toList();
  }

  Widget _togglePill({
    required String label,
    required bool selected,
    required VoidCallback onTap,
    bool fullWidth = true,
  }) {
    return GestureDetector(
      onTap: onTap,
      child: Container(
        width: fullWidth ? double.infinity : null,
        padding: EdgeInsets.symmetric(horizontal: 12, vertical: fullWidth ? 9 : 6),
        decoration: BoxDecoration(
          color: selected ? AppColors.accent : AppColors.surfaceHover,
          borderRadius: BorderRadius.circular(AppSpacing.radius),
        ),
        child: Text(
          label,
          textAlign: TextAlign.center,
          style: TextStyle(
            color: selected ? AppColors.text : AppColors.textMuted,
            fontSize: 13,
            fontWeight: FontWeight.bold,
          ),
        ),
      ),
    );
  }

  Widget _sectionHeader(String title, {String? statusText, Widget? trailing}) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: Row(
        mainAxisAlignment: MainAxisAlignment.spaceBetween,
        children: [
          Expanded(
            child: Text(
              title,
              style: const TextStyle(color: AppColors.text, fontSize: 19, fontWeight: FontWeight.bold),
            ),
          ),
          if (statusText != null)
            Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                const Icon(LucideIcons.check, size: 15, color: AppColors.success),
                const SizedBox(width: 4),
                Text(statusText, style: const TextStyle(color: AppColors.success, fontSize: 13, fontWeight: FontWeight.w600)),
              ],
            ),
          if (trailing != null) trailing,
        ],
      ),
    );
  }

  Widget _labeledField(String label, TextEditingController controller, {bool isNumber = false, String? hint}) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(label, style: const TextStyle(color: AppColors.textMuted, fontWeight: FontWeight.w600, fontSize: 13)),
          const SizedBox(height: 6),
          TextField(
            controller: controller,
            keyboardType: isNumber ? const TextInputType.numberWithOptions(decimal: true) : TextInputType.text,
            decoration: InputDecoration(hintText: hint ?? label),
          ),
        ],
      ),
    );
  }

  Widget _buildPasswordInput({
    required TextEditingController controller,
    required String hint,
    required bool obscure,
    required VoidCallback onToggleObscure,
    bool readOnly = false,
    ValueChanged<String>? onChanged,
  }) {
    return TextField(
      controller: controller,
      obscureText: obscure,
      readOnly: readOnly,
      onChanged: onChanged,
      decoration: InputDecoration(
        hintText: hint,
        suffixIcon: IconButton(
          icon: Icon(obscure ? LucideIcons.eye : LucideIcons.eyeOff, size: 18, color: AppColors.textMuted),
          onPressed: onToggleObscure,
        ),
      ),
    );
  }

  Widget _buildPasswordStrengthBar() {
    final (ratio, label, color) = _evaluatePasswordStrength(_newPasswordCtrl.text);
    if (ratio == 0.0) return const SizedBox.shrink();
    return Padding(
      padding: const EdgeInsets.only(top: 6),
      child: Row(
        children: [
          Expanded(
            child: ClipRRect(
              borderRadius: BorderRadius.circular(3),
              child: LinearProgressIndicator(
                value: ratio,
                minHeight: 4,
                backgroundColor: AppColors.strengthTrack,
                color: color,
              ),
            ),
          ),
          const SizedBox(width: 8),
          Text(label, style: TextStyle(color: color, fontSize: 12, fontWeight: FontWeight.w600)),
        ],
      ),
    );
  }

  void _confirmSignOut() {
    showDialog(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Sign Out'),
        content: const Text('Are you sure you want to sign out of Heirloom?'),
        actions: [
          TextButton(onPressed: () => Navigator.of(ctx).pop(), child: const Text('Cancel')),
          ElevatedButton(
            onPressed: () async {
              Navigator.of(ctx).pop();
              await ref.read(appSessionProvider.notifier).signOut();
              if (mounted) context.go('/login');
            },
            child: const Text('Sign Out'),
          ),
        ],
      ),
    );
  }
}
