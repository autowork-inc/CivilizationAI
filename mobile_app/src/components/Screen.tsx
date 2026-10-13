import type { ReactNode } from 'react';
import { KeyboardAvoidingView, Platform, ScrollView, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

/** Dark full-screen container for the auth screens: safe area, keyboard avoidance, scroll on small phones. */
export function Screen({ children }: { children: ReactNode }) {
  return (
    <SafeAreaView className="flex-1 bg-slate-950">
      {/* soft accent glows behind the content */}
      <View pointerEvents="none" className="absolute -right-24 -top-24 h-72 w-72 rounded-full bg-indigo-600/20" />
      <View pointerEvents="none" className="absolute -left-32 bottom-10 h-72 w-72 rounded-full bg-blue-600/10" />
      <KeyboardAvoidingView className="flex-1" behavior={Platform.OS === 'ios' ? 'padding' : 'height'}>
        <ScrollView
          contentContainerClassName="flex-grow justify-center px-6 py-8"
          keyboardShouldPersistTaps="handled"
          showsVerticalScrollIndicator={false}>
          {children}
        </ScrollView>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}
