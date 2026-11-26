import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:lucide_icons/lucide_icons.dart';
import '../../core/api_client.dart';
import '../../core/storage/device_storage.dart';
import '../../core/theme.dart';

class SetupScreen extends ConsumerStatefulWidget {
  const SetupScreen({super.key});

  @override
  ConsumerState<SetupScreen> createState() => _SetupScreenState();
}

class _SetupScreenState extends ConsumerState<SetupScreen> {
  final _urlController = TextEditingController();
  bool _isLoading = false;
  String? _error;

  @override
  void dispose() {
    _urlController.dispose();
    super.dispose();
  }

  Future<void> _connect([String? overrideUrl]) async {
    final target = (overrideUrl ?? _urlController.text).trim();
    if (target.isEmpty) {
      setState(() => _error = 'Please enter a server URL');
      return;
    }

    setState(() {
      _isLoading = true;
      _error = null;
    });

    try {
      await ref.read(appSessionProvider.notifier).connectAndSaveServer(target);
      final updated = ref.read(appSessionProvider);
      if (!mounted) return;
      if (updated.isAuthenticated) {
        context.go(updated.activeProfileId != null ? '/' : '/profiles');
      } else {
        context.go('/login');
      }
    } catch (e) {
      if (mounted) {
        setState(() {
          _error = formatApiError(e, fallback: 'Unable to reach server. Verify URL and network connection.');
        });
      }
    } finally {
      if (mounted) {
        setState(() => _isLoading = false);
      }
    }
  }

  Future<void> _selectSavedServer(SavedServer server) async {
    setState(() {
      _isLoading = true;
      _error = null;
    });
    try {
      await ref.read(appSessionProvider.notifier).switchServer(server);
      final updated = ref.read(appSessionProvider);
      if (!mounted) return;
      if (updated.isAuthenticated) {
        context.go(updated.activeProfileId != null ? '/' : '/profiles');
      } else {
        context.go('/login');
      }
    } catch (e) {
      if (mounted) {
        setState(() => _error = formatApiError(e, fallback: 'Could not switch server.'));
      }
    } finally {
      if (mounted) {
        setState(() => _isLoading = false);
      }
    }
  }

  Future<void> _forgetServer(SavedServer server) async {
    await ref.read(appSessionProvider.notifier).removeServer(server.id);
    if (mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Removed ${server.name}')),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    final session = ref.watch(appSessionProvider);
    final savedServers = session.savedServers;

    return Scaffold(
      appBar: context.canPop()
          ? AppBar(
              title: const Text('Servers'),
              leading: IconButton(
                icon: const Icon(LucideIcons.arrowLeft),
                onPressed: () => context.pop(),
              ),
            )
          : null,
      body: Center(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(24.0),
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 440),
            child: Column(
              mainAxisAlignment: MainAxisAlignment.center,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                const Icon(LucideIcons.server, size: 56, color: AppColors.accent),
                const SizedBox(height: 16),
                const Text(
                  'Connect to Heirloom',
                  textAlign: TextAlign.center,
                  style: TextStyle(fontSize: 28, fontWeight: FontWeight.bold, color: AppColors.text),
                ),
                const SizedBox(height: 8),
                const Text(
                  'Enter the URL of your Heirloom server or choose a saved server on this device.',
                  textAlign: TextAlign.center,
                  style: TextStyle(color: AppColors.textMuted),
                ),
                const SizedBox(height: 28),
                if (savedServers.isNotEmpty) ...[
                  const Text(
                    'Saved Servers',
                    style: TextStyle(fontSize: 13, fontWeight: FontWeight.w600, color: AppColors.textMuted),
                  ),
                  const SizedBox(height: 8),
                  ...savedServers.map((srv) {
                    final isActive = session.activeServer?.id == srv.id;
                    return Container(
                      margin: const EdgeInsets.only(bottom: 8),
                      decoration: BoxDecoration(
                        color: AppColors.surface,
                        borderRadius: BorderRadius.circular(AppSpacing.radius),
                      ),
                      child: ListTile(
                        leading: Icon(
                          LucideIcons.hardDrive,
                          color: isActive ? AppColors.accent : AppColors.textMuted,
                        ),
                        title: Text(
                          srv.name,
                          style: const TextStyle(fontWeight: FontWeight.w600, color: AppColors.text),
                        ),
                        subtitle: Text(
                          '${srv.url}${srv.lastUsername != null ? ' • ${srv.lastUsername}' : ''}',
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(color: AppColors.textMuted, fontSize: 12),
                        ),
                        trailing: PopupMenuButton<String>(
                          icon: const Icon(LucideIcons.moreVertical, color: AppColors.textMuted),
                          color: AppColors.surface,
                          onSelected: (action) {
                            if (action == 'connect') _selectSavedServer(srv);
                            if (action == 'edit') _urlController.text = srv.url;
                            if (action == 'forget') _forgetServer(srv);
                          },
                          itemBuilder: (ctx) => const [
                            PopupMenuItem(value: 'connect', child: Text('Connect')),
                            PopupMenuItem(value: 'edit', child: Text('Edit URL')),
                            PopupMenuItem(
                              value: 'forget',
                              child: Text('Forget Server', style: TextStyle(color: AppColors.failure)),
                            ),
                          ],
                        ),
                        onTap: _isLoading ? null : () => _selectSavedServer(srv),
                      ),
                    );
                  }),
                  const SizedBox(height: 20),
                  const Divider(),
                  const SizedBox(height: 20),
                ],
                if (_error != null)
                  Container(
                    padding: const EdgeInsets.all(12),
                    margin: const EdgeInsets.only(bottom: 18),
                    decoration: BoxDecoration(
                      color: AppColors.accentTransparent,
                      borderRadius: BorderRadius.circular(AppSpacing.radius),
                    ),
                    child: Text(
                      _error!,
                      style: const TextStyle(color: AppColors.accent),
                      textAlign: TextAlign.center,
                    ),
                  ),
                TextField(
                  controller: _urlController,
                  decoration: const InputDecoration(
                    labelText: 'Server URL',
                    hintText: 'http://192.168.1.x:42069',
                    prefixIcon: Icon(LucideIcons.link),
                  ),
                  keyboardType: TextInputType.url,
                  textInputAction: TextInputAction.go,
                  onSubmitted: (_) => _connect(),
                ),
                const SizedBox(height: 16),
                ElevatedButton(
                  onPressed: _isLoading ? null : () => _connect(),
                  child: _isLoading
                      ? const SizedBox(
                          height: 20,
                          width: 20,
                          child: CircularProgressIndicator(strokeWidth: 2, color: AppColors.text),
                        )
                      : const Text('Connect', style: TextStyle(fontSize: 16, fontWeight: FontWeight.bold)),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
