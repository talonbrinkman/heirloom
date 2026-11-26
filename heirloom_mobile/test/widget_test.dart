import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:heirloom_mobile/core/api_client.dart';
import 'package:heirloom_mobile/core/storage/device_storage.dart';
import 'package:heirloom_mobile/core/theme.dart';
import 'package:heirloom_mobile/features/auth/login_screen.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  testWidgets('LoginScreen defaults Keep me signed in checkbox to enabled (true)', (WidgetTester tester) async {
    SharedPreferences.setMockInitialValues({});
    final prefs = await SharedPreferences.getInstance();
    final secure = InMemorySecureKeyValueStore();
    final storage = DeviceStorage(prefs, secure);
    final srv = await storage.saveServer(
      SavedServer(
        id: 'test',
        name: 'Test Server',
        url: 'http://localhost:42069',
        lastUsedAt: DateTime.now().toUtc(),
      ),
    );
    await storage.setActiveServerId(srv.id);

    final container = ProviderContainer(
      overrides: [
        sharedPreferencesProvider.overrideWithValue(prefs),
        secureKeyValueStoreProvider.overrideWithValue(secure),
      ],
    );
    await container.read(appSessionProvider.notifier).init(refreshRemote: false);

    await tester.pumpWidget(
      UncontrolledProviderScope(
        container: container,
        child: MaterialApp(
          theme: AppTheme.darkTheme,
          home: const LoginScreen(),
        ),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('Keep me signed in'), findsOneWidget);
    final checkbox = tester.widget<Checkbox>(find.byType(Checkbox));
    expect(checkbox.value, isTrue);
  });
}
