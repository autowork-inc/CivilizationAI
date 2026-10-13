import { useState } from 'react';
import { Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { Button } from '@/components/Button';
import { useAuth } from '@/context/AuthContext';

/** ChatScreen: a stub for now; the real chat (/api/v1/mobile/chat/stream) comes next. */
export default function ChatScreen() {
  const { logout } = useAuth();
  const [busy, setBusy] = useState(false);

  async function signOut() {
    setBusy(true);
    await logout(); // the root layout swaps (app) for (auth)
  }

  return (
    <SafeAreaView className="flex-1 bg-neutral-950">
      <View className="flex-1 items-center justify-center px-6">
        <Text className="mb-8 text-center text-2xl font-semibold text-neutral-50">Вы успешно вошли!</Text>
        <View className="w-full">
          <Button title="Выйти" variant="secondary" onPress={signOut} loading={busy} />
        </View>
      </View>
    </SafeAreaView>
  );
}
